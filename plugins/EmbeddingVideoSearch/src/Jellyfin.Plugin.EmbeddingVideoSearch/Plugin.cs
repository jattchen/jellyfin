using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.MediaEncoding;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Controller.Session;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public sealed class PluginConfiguration : BasePluginConfiguration
{
}

public sealed class Plugin : BasePlugin<PluginConfiguration>
{
    public Plugin(IApplicationPaths applicationPaths, IXmlSerializer xmlSerializer) : base(applicationPaths, xmlSerializer)
    {
    }

    public override string Name => "Embedding Video Search";

    public override string Description => "Search video pictures and dialogue by meaning on Jellyfin 12.2.0.";

    public override void OnUninstalling()
    {
        PluginLifetime.RequestStop();
    }
}

public sealed class PluginServiceRegistrator : IPluginServiceRegistrator
{
    public void RegisterServices(IServiceCollection serviceCollection, IServerApplicationHost applicationHost)
    {
        serviceCollection.AddSingleton<MediaPlaybackStore>();
        serviceCollection.AddSingleton(serviceProvider =>
        {
            var paths = serviceProvider.GetRequiredService<IApplicationPaths>();
            var library = serviceProvider.GetRequiredService<ILibraryManager>();
            var users = serviceProvider.GetRequiredService<IUserManager>();
            var sessions = serviceProvider.GetRequiredService<ISessionManager>();
            var encoder = serviceProvider.GetRequiredService<IMediaEncoder>();
            var loggerFactory = serviceProvider.GetRequiredService<ILoggerFactory>();
            var directory = ApiV1.IndexDirectory(paths.DataPath);
            var store = IndexStore.Open(directory);
            var logger = loggerFactory.CreateLogger("EmbeddingVideoSearch");
            logger.LogInformation(
                "Opened embedding video search index. schemaReadOnly={SchemaReadOnly} writer={Writer}",
                store.SchemaReadOnly,
                store.HasWriterLock);
            var http = new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
            var dispatcher = new ModelDispatcher(http, logger);
            dispatcher.AllowResume = observation =>
            {
                var active = store.ActiveSignature();
                if (active is null)
                {
                    return true;
                }

                var matches = observation.IsExpectedModel && IndexStore.SignaturesMatch(active, observation);
                if (!matches)
                {
                    store.SetIncompatible(true);
                }

                return matches;
            };
            var client = new HttpEmbeddingClient(dispatcher: dispatcher);
            var catalog = new JellyfinLibraryCatalog(library);
            var index = new JellyfinIndexCatalog(library, sessions, encoder);
            var clipper = new FfmpegWindowClipper(() => index.FfmpegPath);
            var coordinator = new IndexCoordinator(store, client, index, clipper, Path.Combine(directory, "tmp"), logger, dispatcher, applicationHost.ApplicationVersion);
            var signals = new JellyfinLibrarySignals(library, coordinator);
            var runtime = new SearchRuntime(store, client, new JellyfinMediaAccess(users, library, store.EnabledLibraryIds), catalog, applicationHost.ApplicationVersion, index, index.IsVideoPlaybackActive);
            return new EmbeddingHost(runtime, new IndexHostedService(coordinator, signals, loggerFactory.CreateLogger<IndexHostedService>()));
        });
        serviceCollection.AddSingleton(serviceProvider => serviceProvider.GetRequiredService<EmbeddingHost>().Runtime);
        serviceCollection.AddSingleton<IHostedService>(serviceProvider => serviceProvider.GetRequiredService<EmbeddingHost>().Worker);
    }
}

public sealed class EmbeddingHost
{
    public EmbeddingHost(SearchRuntime runtime, IndexHostedService worker)
    {
        Runtime = runtime;
        Worker = worker;
    }

    public SearchRuntime Runtime { get; }
    public IndexHostedService Worker { get; }
}

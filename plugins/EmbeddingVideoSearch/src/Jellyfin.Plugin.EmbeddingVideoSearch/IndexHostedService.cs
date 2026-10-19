using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public static class PluginLifetime
{
    private static CancellationTokenSource _stop = new();

    public static CancellationToken Token => _stop.Token;

    public static void RequestStop() => _stop.Cancel();
}

public sealed class IndexHostedService : IHostedService, IDisposable
{
    private readonly IndexCoordinator _coordinator;
    private readonly JellyfinLibrarySignals _signals;
    private readonly ILogger _logger;
    private CancellationTokenSource? _run;
    private Task? _loop;

    public IndexHostedService(IndexCoordinator coordinator, JellyfinLibrarySignals signals, ILogger<IndexHostedService> logger)
    {
        _coordinator = coordinator;
        _signals = signals;
        _logger = logger;
    }

    public Task StartAsync(CancellationToken cancellationToken)
    {
        try
        {
            _coordinator.Start();
            if (_coordinator.DisabledReason is not null)
            {
                _logger.LogInformation("index worker left idle. reason={Reason}", _coordinator.DisabledReason);
                return Task.CompletedTask;
            }

            _signals.Start();
            _run = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, PluginLifetime.Token);
            _loop = Task.Run(() => Loop(_run.Token), CancellationToken.None);
            return Task.CompletedTask;
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "index worker left idle because startup failed");
            return Task.CompletedTask;
        }
    }

    public async Task StopAsync(CancellationToken cancellationToken)
    {
        _signals.Dispose();
        if (_run is not null)
        {
            await _run.CancelAsync().ConfigureAwait(false);
        }

        if (_loop is not null)
        {
            await _loop.WaitAsync(cancellationToken).ConfigureAwait(false);
        }
    }

    public void Dispose()
    {
        _signals.Dispose();
        _run?.Dispose();
    }

    private async Task Loop(CancellationToken cancellationToken)
    {
        while (!cancellationToken.IsCancellationRequested)
        {
            try
            {
                _coordinator.ProcessEvents();
                var step = await _coordinator.DispatchOneAsync(cancellationToken).ConfigureAwait(false);
                var delay = step == DispatchStep.Backoff ? _coordinator.BackoffDelay : TimeSpan.FromSeconds(1);
                await Task.Delay(delay, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "index worker step failed");
                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(2), cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }
        }
    }
}

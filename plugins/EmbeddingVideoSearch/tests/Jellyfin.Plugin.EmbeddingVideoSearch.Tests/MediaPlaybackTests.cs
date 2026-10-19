using Xunit;

namespace Jellyfin.Plugin.EmbeddingVideoSearch.Tests;

public class MediaPlaybackTests
{
    private static readonly Guid Item = Guid.Parse("7bd8bb2a-11a3-7d25-9efb-9c18afac0339");
    private static readonly Uri Origin = new("http://127.0.0.1:18096/");

    [Fact]
    public void Generated_media_reference_strips_all_token_spellings_without_changing_playback_parameters()
    {
        var uri = MediaPlaybackStore.SafeUpstream(Origin, Item,
            $"/videos/{Item:D}/master.m3u8?ApiKey=secret&api_key=secret2&Access-Token=secret3&Token=secret4&MediaSourceId=source&VideoCodec=copy");
        Assert.DoesNotContain("secret", uri.AbsoluteUri);
        Assert.Contains("MediaSourceId=source", uri.Query);
        Assert.Contains("VideoCodec=copy", uri.Query);
    }

    [Theory]
    [InlineData("http://evil.example/videos/7bd8bb2a-11a3-7d25-9efb-9c18afac0339/main.m3u8")]
    [InlineData("http://127.0.0.1:18780/videos/7bd8bb2a-11a3-7d25-9efb-9c18afac0339/main.m3u8")]
    [InlineData("http://user:password@127.0.0.1:18096/videos/7bd8bb2a-11a3-7d25-9efb-9c18afac0339/main.m3u8")]
    [InlineData("/videos/00000000-0000-0000-0000-000000000001/main.m3u8")]
    [InlineData("/videos/7bd8bb2a-11a3-7d25-9efb-9c18afac0339/../../System/Info")]
    [InlineData("/videos/7bd8bb2a-11a3-7d25-9efb-9c18afac0339/attachment.srt")]
    public void Media_reference_cannot_change_origin_item_or_resource_kind(string reference)
        => Assert.Throws<InvalidDataException>(() => MediaPlaybackStore.SafeUpstream(Origin, Item, reference));

    [Fact]
    public void Media_reference_accepts_both_guid_formats_and_bound_hls_segments()
    {
        Assert.EndsWith("/hls1/main/42.mp4", MediaPlaybackStore.SafeUpstream(Origin, Item, $"/videos/{Item:N}/hls1/main/42.mp4").AbsolutePath);
    }

    [Fact]
    public void Playback_ownership_distinguishes_two_logins_of_the_same_user()
        => Assert.NotEqual(MediaPlaybackStore.LoginKey("first-login"), MediaPlaybackStore.LoginKey("second-login"));

    [Fact]
    public async Task Failed_host_cleanup_revokes_media_but_retains_a_retryable_record()
    {
        var store = new MediaPlaybackStore();
        var session = new MediaPlaybackSession
        {
            UserId = Guid.NewGuid(), LoginKey = "login", SegmentId = Guid.NewGuid(), ItemId = Item,
            MediaSourceId = "source", ContentVersion = "content", BindingVersion = "binding",
            Origin = Origin, HostPlaySessionId = "host-session", DeviceId = "test-device"
        };
        store.Add(session);
        using var transport = new HttpClient(new CleanupHandler());
        Assert.False(await store.StopAsync(session, "test-authorization", CancellationToken.None, transport));
        Assert.True(session.Revoked);
        Assert.Same(session, store.Find(session.Id));
        Assert.True(await store.StopAsync(session, "test-authorization", CancellationToken.None, transport));
        Assert.Null(store.Find(session.Id));
    }

    private sealed class CleanupHandler : HttpMessageHandler
    {
        private int _calls;
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Assert.Equal(HttpMethod.Delete, request.Method);
            Assert.Equal("/Videos/ActiveEncodings", request.RequestUri!.AbsolutePath);
            Assert.Contains("host-session", request.RequestUri.Query);
            return Task.FromResult(new HttpResponseMessage(++_calls == 1 ? System.Net.HttpStatusCode.InternalServerError : System.Net.HttpStatusCode.NoContent));
        }
    }
}

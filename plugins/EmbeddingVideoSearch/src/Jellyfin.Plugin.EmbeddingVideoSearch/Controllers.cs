using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public abstract class PluginControllerBase : ControllerBase
{
    private readonly SearchRuntime _runtime;

    protected PluginControllerBase(SearchRuntime runtime)
    {
        _runtime = runtime;
    }

    protected SearchRuntime Runtime => _runtime;

    protected ActionResult JsonBody(object payload)
        => Content(JsonProtocol.Serialize(payload), "application/json; charset=utf-8");

    protected ActionResult Error(int status, string code, string message)
        => new ContentResult
        {
            StatusCode = status,
            ContentType = "application/json; charset=utf-8",
            Content = JsonProtocol.Serialize(new ErrorBody
            {
                Error = new ErrorDetail { Code = code, Message = message }
            })
        };

    protected bool TryUser(out Guid userId, out ActionResult error)
    {
        userId = Guid.Empty;
        var claim = User.FindFirst(ApiV1.UserIdClaim)?.Value;
        if (!Guid.TryParse(claim, out userId) || userId == Guid.Empty)
        {
            error = Error(StatusCodes.Status401Unauthorized, "unauthorized", "A signed-in user is required.");
            return false;
        }

        error = null!;
        return true;
    }

    protected async Task<string> ReadBodyAsync(CancellationToken cancellationToken)
    {
        if (Request.ContentLength is > RequestBounds.MaxBytes)
        {
            throw new SearchInputException("invalidRequest", "Request body is too large.");
        }

        return await RequestBounds.ReadUtf8Async(Request.Body, cancellationToken).ConfigureAwait(false);
    }

    protected ActionResult FromException(Exception exception)
    {
        return exception switch
        {
            SearchAuthException auth => Error(auth.Status, auth.Code, auth.Message),
            JsonException => Error(StatusCodes.Status400BadRequest, "invalidRequest", "The request JSON could not be read."),
            SearchInputException input => Error(input.Code == "invalidRequest" ? StatusCodes.Status400BadRequest : StatusCodes.Status409Conflict, input.Code, input.Message),
            _ => Error(StatusCodes.Status500InternalServerError, "internal", "The plugin could not complete the request.")
        };
    }
}

[ApiController]
[Authorize]
[Route(ApiV1.Route)]
public sealed class EmbeddingVideoSearchController : PluginControllerBase
{
    public EmbeddingVideoSearchController(SearchRuntime runtime) : base(runtime)
    {
    }

    [HttpGet("capabilities")]
    public async Task<ActionResult> Capabilities(CancellationToken cancellationToken)
    {
        if (!TryUser(out var userId, out var error))
        {
            return error;
        }

        try
        {
            return JsonBody(await Runtime.CapabilitiesAsync(userId, cancellationToken).ConfigureAwait(false));
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException or HttpRequestException)
        {
            return FromException(ex);
        }
    }

    [HttpPost("search")]
    public async Task<ActionResult> Search(CancellationToken cancellationToken)
    {
        if (!TryUser(out var userId, out var error))
        {
            return error;
        }

        try
        {
            var body = await ReadBodyAsync(cancellationToken).ConfigureAwait(false);
            var request = string.IsNullOrWhiteSpace(body) ? null : JsonProtocol.Deserialize<SearchRequest>(body);
            if (request is null)
            {
                return Error(StatusCodes.Status400BadRequest, "invalidRequest", "Search JSON is required.");
            }

            return JsonBody(await Runtime.SearchAsync(userId, request, cancellationToken).ConfigureAwait(false));
        }
        catch (SearchInputException ex) when (ex.Code == "invalidRequest")
        {
            return Error(StatusCodes.Status400BadRequest, ex.Code, ex.Message);
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException or JsonException or HttpRequestException)
        {
            return FromException(ex);
        }
    }

    [HttpPost("similar")]
    public async Task<ActionResult> Similar(CancellationToken cancellationToken)
    {
        if (!TryUser(out var userId, out var error))
        {
            return error;
        }

        try
        {
            var body = await ReadBodyAsync(cancellationToken).ConfigureAwait(false);
            var request = string.IsNullOrWhiteSpace(body) ? null : JsonProtocol.Deserialize<SimilarRequest>(body);
            if (request is null)
            {
                return Error(StatusCodes.Status400BadRequest, "invalidRequest", "Similar JSON is required.");
            }

            return JsonBody(Runtime.Similar(userId, request, cancellationToken));
        }
        catch (SearchInputException ex) when (ex.Code == "invalidRequest")
        {
            return Error(StatusCodes.Status400BadRequest, ex.Code, ex.Message);
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException or JsonException)
        {
            return FromException(ex);
        }
    }

    [HttpGet("segments/{segmentId}")]
    public ActionResult Segment([FromRoute] string segmentId)
    {
        if (!TryUser(out var userId, out var error))
        {
            return error;
        }

        if (!FlexibleGuid.TryRead(segmentId, out var id))
        {
            return Error(StatusCodes.Status404NotFound, "notFound", "Segment was not found.");
        }

        try
        {
            var decision = Runtime.GetSegment(userId, id);
            if (decision.StatusCode == StatusCodes.Status200OK && decision.Body is not null)
            {
                return JsonBody(decision.Body);
            }

            return Error(decision.StatusCode, decision.Code ?? "notFound", decision.Message ?? "Segment was not found.");
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException)
        {
            return FromException(ex);
        }
    }
}

[ApiController]
[Authorize]
[Authorize(Policy = MediaBrowser.Common.Api.Policies.RequiresElevation)]
[Route(ApiV1.AdminRoute)]
public sealed class EmbeddingVideoSearchAdminController : PluginControllerBase
{
    public EmbeddingVideoSearchAdminController(SearchRuntime runtime) : base(runtime)
    {
    }

    [HttpGet("status")]
    public async Task<ActionResult> Status(CancellationToken cancellationToken)
    {
        if (!TryUser(out var userId, out var error))
        {
            return error;
        }

        try
        {
            return JsonBody(await Runtime.AdminStatusAsync(userId, cancellationToken).ConfigureAwait(false));
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException or HttpRequestException)
        {
            return FromException(ex);
        }
    }

    [HttpPut("libraries")]
    public async Task<ActionResult> Libraries(CancellationToken cancellationToken)
    {
        if (!TryUser(out var userId, out var error))
        {
            return error;
        }

        try
        {
            var body = await ReadBodyAsync(cancellationToken).ConfigureAwait(false);
            var request = JsonProtocol.Deserialize<LibrarySelectionRequest>(body) ?? new LibrarySelectionRequest();
            Runtime.SetLibraries(userId, request.LibraryIds ?? []);
            return JsonBody(await Runtime.AdminStatusAsync(userId, cancellationToken).ConfigureAwait(false));
        }
        catch (SearchInputException ex) when (ex.Code == "invalidRequest")
        {
            return Error(StatusCodes.Status400BadRequest, ex.Code, ex.Message);
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException or JsonException)
        {
            return FromException(ex);
        }
    }

    [HttpPut("settings")]
    public async Task<ActionResult> Settings(CancellationToken cancellationToken)
    {
        if (!TryUser(out var userId, out var error))
        {
            return error;
        }

        try
        {
            var body = await ReadBodyAsync(cancellationToken).ConfigureAwait(false);
            var request = JsonProtocol.Deserialize<SettingsRequest>(body);
            if (request is null)
            {
                return Error(StatusCodes.Status400BadRequest, "invalidRequest", "Settings JSON is required.");
            }

            Runtime.UpdateSettings(userId, request);
            return JsonBody(await Runtime.AdminStatusAsync(userId, cancellationToken).ConfigureAwait(false));
        }
        catch (SearchInputException ex) when (ex.Code == "invalidRequest")
        {
            return Error(StatusCodes.Status400BadRequest, ex.Code, ex.Message);
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException or JsonException)
        {
            return FromException(ex);
        }
    }

    [HttpPost("pause")]
    public async Task<ActionResult> Pause(CancellationToken cancellationToken)
    {
        if (!TryUser(out var userId, out var error))
        {
            return error;
        }

        try
        {
            Runtime.Pause(userId);
            return JsonBody(await Runtime.AdminStatusAsync(userId, cancellationToken).ConfigureAwait(false));
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException)
        {
            return FromException(ex);
        }
    }

    [HttpPost("resume")]
    public async Task<ActionResult> Resume(CancellationToken cancellationToken)
    {
        if (!TryUser(out var userId, out var error))
        {
            return error;
        }

        try
        {
            Runtime.Resume(userId);
            return JsonBody(await Runtime.AdminStatusAsync(userId, cancellationToken).ConfigureAwait(false));
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException)
        {
            return FromException(ex);
        }
    }

    [HttpPost("retryFailed")]
    public async Task<ActionResult> RetryFailed(CancellationToken cancellationToken)
    {
        if (!TryUser(out var userId, out var error))
        {
            return error;
        }

        try
        {
            return JsonBody(await Runtime.RetryFailedAsync(userId, cancellationToken).ConfigureAwait(false));
        }
        catch (SearchInputException ex) when (ex.Code is "hostIncompatible" or "schemaNewer" or "writerLocked")
        {
            return Error(StatusCodes.Status409Conflict, ex.Code, ex.Message);
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException)
        {
            return FromException(ex);
        }
    }

    [HttpPost("rebuild")]
    public async Task<ActionResult> Rebuild(CancellationToken cancellationToken)
    {
        if (!TryUser(out var userId, out var error))
        {
            return error;
        }

        try
        {
            return JsonBody(await Runtime.RebuildAsync(userId, cancellationToken).ConfigureAwait(false));
        }
        catch (SearchInputException ex) when (ex.Code is "modelOffline" or "incompatible" or "alreadyCompatible" or "schemaNewer" or "hostIncompatible")
        {
            return Error(StatusCodes.Status409Conflict, ex.Code, ex.Message);
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException)
        {
            return FromException(ex);
        }
    }
}

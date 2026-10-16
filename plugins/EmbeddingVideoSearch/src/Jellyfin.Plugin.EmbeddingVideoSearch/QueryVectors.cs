namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public sealed partial class SearchRuntime
{
    private readonly object _queryVectorGate = new();
    private readonly Dictionary<string, EmbeddingCall> _queryVectors = new(StringComparer.Ordinal);
    private readonly Queue<string> _queryVectorOrder = new();
    private const int QueryVectorCapacity = 32;

    /// <summary>
    /// Returning from native playback remounts the search route. Reuse only a completed query vector,
    /// scoped to this user and exact model/task/service signature. Never cache search results or authorization.
    /// Pending, failed, cancelled and unknown calls are not shared or retried here.
    /// </summary>
    private async Task<EmbeddingCall> CandidateQueryAsync(Guid userId, SearchRequest request, string query,
        StoredSignature signature, CancellationToken cancellationToken)
    {
        if (cancellationToken.IsCancellationRequested)
        {
            return new EmbeddingCall { ErrorCode = "cancelled" };
        }

        var refusal = await ValidateBeforeQuery(userId, request, signature).ConfigureAwait(false);
        if (refusal is not null)
        {
            return refusal;
        }

        var baseUrl = _store.EmbeddingBaseUrl;
        var key = string.Join('\n', userId, baseUrl, signature.Model, signature.Revision, signature.ProcessingId,
            signature.Dimension, signature.Normalized, "task=query", query);
        lock (_queryVectorGate)
        {
            if (_queryVectors.TryGetValue(key, out var cached))
            {
                return cached;
            }
        }

        var call = await _embeddings.EmbedTextAsync(baseUrl, query, cancellationToken,
            () => ValidateBeforeQuery(userId, request, signature)).ConfigureAwait(false);
        if (call.Success && call.Vector is not null && !cancellationToken.IsCancellationRequested
            && !call.CompletionUnknown && _store.ActiveGeneration == signature.Generation
            && IndexStore.SignaturesMatch(signature, call.Observation))
        {
            lock (_queryVectorGate)
            {
                if (!_queryVectors.ContainsKey(key))
                {
                    while (_queryVectors.Count >= QueryVectorCapacity)
                    {
                        _queryVectors.Remove(_queryVectorOrder.Dequeue());
                    }

                    _queryVectors.Add(key, new EmbeddingCall
                    {
                        Success = true, Vector = call.Vector.ToArray(), Observation = call.Observation, Processing = call.Processing
                    });
                    _queryVectorOrder.Enqueue(key);
                }
            }
        }

        return call;
    }
}

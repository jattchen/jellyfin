#!/bin/zsh
# Build and test from this directory so its global.json pins SDK 10.0.402.
set -euo pipefail
cd "$(dirname "$0")"
export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1

if [[ -z "${DOTNET:-}" ]]; then
  if command -v dotnet >/dev/null 2>&1; then
    DOTNET="$(command -v dotnet)"
  elif [[ -x /Users/mac/.local/share/jellyfin-video-search-tools/dotnet/10.0.402/dotnet ]]; then
    DOTNET="/Users/mac/.local/share/jellyfin-video-search-tools/dotnet/10.0.402/dotnet"
  else
    echo "dotnet was not found. Set DOTNET to the SDK executable." >&2
    exit 1
  fi
fi

"$DOTNET" --version
"$DOTNET" test EmbeddingVideoSearch.slnx -c Release --nologo
"$DOTNET" publish src/Jellyfin.Plugin.EmbeddingVideoSearch/Jellyfin.Plugin.EmbeddingVideoSearch.csproj -c Release -o artifacts/publish --nologo

stage() {
  local dest="$1"
  local include_sqlite="$2"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp artifacts/publish/Jellyfin.Plugin.EmbeddingVideoSearch.dll "$dest/"
  cp artifacts/publish/meta.json "$dest/"
  if [[ "$include_sqlite" == "yes" ]]; then
    cp artifacts/publish/Microsoft.Data.Sqlite.dll "$dest/"
    cp artifacts/publish/SQLitePCLRaw.core.dll "$dest/"
    cp artifacts/publish/SQLitePCLRaw.batteries_v2.dll "$dest/"
    cp artifacts/publish/SQLitePCLRaw.provider.e_sqlite3.dll "$dest/"
    local native="artifacts/publish/runtimes/osx-arm64/native/libe_sqlite3.dylib"
    if [[ ! -f "$native" ]]; then
      echo "missing osx-arm64 sqlite native library" >&2
      exit 1
    fi
    cp "$native" "$dest/libe_sqlite3.dylib"
  fi
}

stage artifacts/plugin-with-sqlite yes
stage artifacts/plugin-host-sqlite no
echo "packages ready"

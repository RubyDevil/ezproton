#!/usr/bin/env bash
# Install, update or uninstall ezproton.
#
#   ./install.sh                     install for everyone into /usr/local/bin (uses sudo)
#   ./install.sh --user              install just for you into ~/.local/bin
#   ./install.sh --no-proton-drive   don't install or update Proton's proton-drive
#   ./install.sh --uninstall [--user]
#
# Without a clone:  curl -fsSL https://raw.githubusercontent.com/RubyDevil/ezproton/main/install.sh | bash -s -- --user
#
# proton-drive: the newest stable release listed in Proton's version.json is installed, checked
# against the SHA-512 published there. If that fails, the release pinned below is installed,
# checked against the SHA-512 recorded in this file.

set -euo pipefail

repo_raw=${EZPROTON_RAW:-https://raw.githubusercontent.com/RubyDevil/ezproton/${EZPROTON_REF:-main}}
manifest_url=https://proton.me/download/drive/cli/version.json
pinned_version=0.9.0
declare -A pinned_sha512=(
    [linux/x64]=3533025ba69ae112b64e3e01fbcc1ad0688136a4043f6cf6a72886967d85fdcd9ec235479c2e113171614be5225bfba93427509a05ca5aa6071d924fa7e91ca8
    [linux/arm64]=c8d5a6b174e57f06d05cb5483400b9963faf58e743b6b33706bb73ecff718047ffce6b439994255b94b5b226b90d2e91bef7418ec96c1c3f5d8bf39b6cd01321
    [linux/x64-musl]=8f99c3bc478ebc2948ca69c8576a48d83b3290d807ca61fac9b0f5d0a91c0d418e595232d67e2ae0173d2188963771afd4292aefafab3f64e808a61c4399d6ed
    [linux/arm64-musl]=4b632e5e694f6d0825aaa57f39f99494aa6a46eac45e90d65daa10b2e46316046b627d3c1145cb9c7d58951735cc6e81c28cca44dda648b411315a28d04b2acf
)

usage() {
    sed -n '2,9p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//' ||
        echo "Usage: install.sh [--user] [--no-proton-drive] [--uninstall]"
}

user=false proton=true uninstall=false
for arg in "$@"; do
    case "$arg" in
        --user) user=true ;;
        --no-proton-drive) proton=false ;;
        --uninstall) uninstall=true ;;
        -h | --help) usage; exit 0 ;;
        *) echo "install.sh: unknown option: $arg" >&2; usage >&2; exit 1 ;;
    esac
done

if $user; then
    bin_dir=${XDG_BIN_HOME:-$HOME/.local/bin}
    sudo=()
else
    bin_dir=/usr/local/bin
    sudo=()
    if ((EUID != 0)); then
        command -v sudo >/dev/null || { echo "sudo is needed to install into $bin_dir (or use --user)." >&2; exit 1; }
        sudo=(sudo)
    fi
fi

say() { printf '%s\n' "$*"; }

if $uninstall; then
    if [[ -e "$bin_dir/ezproton" ]]; then
        "${sudo[@]}" rm -f -- "$bin_dir/ezproton"
        say "Removed $bin_dir/ezproton."
    else
        say "ezproton isn't installed in $bin_dir."
    fi
    cat <<EOF
Left in place:
  your settings and records   ~/.config/ezproton  ~/.local/state/ezproton
  your files on Proton Drive
  proton-drive                $(command -v proton-drive || echo "(not found)")
To remove those too: proton-drive auth logout, then delete the two folders above and the
proton-drive file.
EOF
    exit 0
fi

temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT

# --- ezproton ---------------------------------------------------------------------------

command -v python3 >/dev/null || { echo "ezproton needs Python 3.9 or newer (python3 was not found)." >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' ||
    { echo "ezproton needs Python 3.9 or newer; this is $(python3 --version)." >&2; exit 1; }

here=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
fi
if [[ -n "$here" && -f "$here/ezproton" ]]; then
    cp -- "$here/ezproton" "$temporary/ezproton"
else
    command -v curl >/dev/null || { echo "curl is needed to download ezproton." >&2; exit 1; }
    say "Downloading ezproton..."
    curl -fsSL --max-time 120 "$repo_raw/ezproton" -o "$temporary/ezproton"
fi
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$temporary/ezproton" ||
    { echo "The downloaded ezproton is not a valid Python file; nothing was installed." >&2; exit 1; }

"${sudo[@]}" mkdir -p -- "$bin_dir"
"${sudo[@]}" install -m 755 -- "$temporary/ezproton" "$bin_dir/ezproton.new"
"${sudo[@]}" mv -f -- "$bin_dir/ezproton.new" "$bin_dir/ezproton"
say "Installed $("$bin_dir/ezproton" --version) at $bin_dir/ezproton."

# --- proton-drive -----------------------------------------------------------------------

version_of() {
    timeout 30 "$1" version 2>/dev/null </dev/null | sed -n 's/.*cli-drive@\([0-9][0-9.]*\).*/\1/p' | head -n 1
}

newest() { printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1; }

install_proton_drive() {
    command -v curl >/dev/null || { echo "curl is needed to download proton-drive." >&2; return 1; }
    local cpu platform
    case "$(uname -m)" in
        x86_64 | amd64) cpu=x64 ;;
        aarch64 | arm64) cpu=arm64 ;;
        *) echo "proton-drive has no Linux build for $(uname -m)." >&2; return 1 ;;
    esac
    platform=linux/$cpu
    compgen -G '/lib/ld-musl-*' >/dev/null && platform+=-musl

    local installed_path installed_version=""
    installed_path=$(command -v proton-drive || true)
    [[ -n "$installed_path" ]] && installed_version=$(version_of "$installed_path")

    # The newest stable release for this platform, as "<version> <url> <sha512>".
    local latest=""
    if curl -fsSL --max-time 30 "$manifest_url" -o "$temporary/version.json" 2>/dev/null; then
        latest=$(python3 -I -c '
import json, sys
platform, path = sys.argv[1], sys.argv[2]
best = None
for release in json.load(open(path)).get("Releases", []):
    if release.get("CategoryName") != "Stable":
        continue
    for item in release.get("Files", []):
        if item.get("Platform") == platform:
            key = tuple(int(p) for p in release["Version"].split(".") if p.isdigit())
            if best is None or key > best[0]:
                best = (key, release["Version"], item["Url"], item["Sha512CheckSum"])
if best:
    print(best[1], best[2], best[3])
' "$platform" "$temporary/version.json" 2>/dev/null || true)
    fi

    local version url sha512 tries=()
    if [[ -n "$latest" ]]; then
        read -r version url sha512 <<<"$latest"
        tries+=("$version $url $sha512")
    else
        say "Could not read Proton's latest proton-drive release; using the pinned $pinned_version."
    fi
    tries+=("$pinned_version https://proton.me/download/drive/cli/$pinned_version/${platform/\//-}/proton-drive ${pinned_sha512[$platform]}")

    for try in "${tries[@]}"; do
        read -r version url sha512 <<<"$try"
        if [[ -n "$installed_version" && "$(newest "$version" "$installed_version")" == "$installed_version" ]]; then
            say "proton-drive $installed_version is already installed at $installed_path; keeping it."
            return 0
        fi
        if [[ "$url" != https://proton.me/download/drive/cli/* || ! "$sha512" =~ ^[0-9a-f]{128}$ ]]; then
            echo "Unexpected download details for proton-drive $version; skipping them." >&2
            continue
        fi
        say "Downloading proton-drive $version ($platform)..."
        if ! curl -fsSL --max-time 900 "$url" -o "$temporary/proton-drive"; then
            echo "Download of proton-drive $version failed." >&2
            continue
        fi
        if [[ "$(sha512sum <"$temporary/proton-drive" | cut -d ' ' -f 1)" != "$sha512" ]]; then
            echo "proton-drive $version failed its SHA-512 check; not installing it." >&2
            continue
        fi
        "${sudo[@]}" install -m 755 -- "$temporary/proton-drive" "$bin_dir/proton-drive.new"
        "${sudo[@]}" mv -f -- "$bin_dir/proton-drive.new" "$bin_dir/proton-drive"
        say "Installed proton-drive $version at $bin_dir/proton-drive."
        if [[ -n "$installed_path" && "$installed_path" != "$bin_dir/proton-drive" ]]; then
            say "Note: an older proton-drive ($installed_version) is at $installed_path. Remove it if your"
            say "PATH finds it before $bin_dir."
        fi
        return 0
    done
    if [[ -n "$installed_version" ]]; then
        echo "Could not update proton-drive; keeping $installed_version at $installed_path." >&2
        return 0
    fi
    echo "Could not install proton-drive. Get it from https://proton.me/download/drive/cli" >&2
    return 1
}

status=0
if $proton; then
    install_proton_drive || status=1
fi

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
        say ""
        say "Note: $bin_dir is not on your PATH, so typing \"ezproton\" won't find it yet."
        say "Add this line to your shell's startup file (such as ~/.bashrc), then open a new terminal:"
        say "  export PATH=\"$bin_dir:\$PATH\""
        ;;
esac

if ((status == 0)); then
    say ""
    say "Next: proton-drive auth login, then ezproton edit backup / ezproton edit sync."
fi
exit "$status"

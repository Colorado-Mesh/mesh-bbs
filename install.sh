#!/bin/sh
# Install only in the invoking user's account; running the service is a separate step.
set -eu

intel_macos_dependencies() {
    if ! printf '#include <stdlib.h>\nint main(void) { return 0; }\n' | clang -x c -fsyntax-only - >/dev/null 2>&1; then
        printf '%s\n' 'Intel Macs need working Xcode Command Line Tools. Run xcode-select --install, then retry.' >&2
        return 1
    fi
    if ! cargo --version >/dev/null 2>&1 || ! rustc --version 2>/dev/null |
        awk '/^rustc / { split($2, v, "."); valid = v[1] > 1 || (v[1] == 1 && v[2] >= 83) } END { exit !valid }'; then
        printf '%s\n' 'Intel Macs need Rust 1.83 or newer. Run brew install rust openssl@3, then retry.' >&2
        return 1
    fi
    mesh_bbs_openssl=${OPENSSL_DIR:-}
    if [ -z "$mesh_bbs_openssl" ] && command -v brew >/dev/null 2>&1; then
        mesh_bbs_openssl=$(brew --prefix openssl@3 2>/dev/null || true)
    fi
    if [ -z "$mesh_bbs_openssl" ] || [ ! -f "$mesh_bbs_openssl/include/openssl/ssl.h" ]; then
        printf '%s\n' 'Intel Macs need OpenSSL development files. Run brew install openssl@3 or set OPENSSL_DIR to your OpenSSL installation.' >&2
        return 1
    fi
    OPENSSL_DIR=$mesh_bbs_openssl
    export OPENSSL_DIR
    printf '%s\n' 'Intel Mac: compiling cryptography from source; the first install takes longer.'
}

main() {
    if [ "$(id -u)" = 0 ]; then
        printf '%s\n' 'Run this installer as your normal user, without sudo.' >&2
        return 1
    fi
    case "$(uname -s)" in
        Linux|Darwin) ;;
        *) printf '%s\n' 'This installer supports Linux and macOS.' >&2; return 1 ;;
    esac

    mesh_bbs_setup=1
    mesh_bbs_ref=${MESH_BBS_REF:-main}
    mesh_bbs_extras=${MESH_BBS_EXTRAS:-reticulum,meshcore,meshtastic}
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --no-setup) mesh_bbs_setup=0 ;;
            --ref)
                if [ "$#" -lt 2 ]; then
                    printf '%s\n' '--ref requires a tag, branch, or commit.' >&2
                    return 1
                fi
                shift
                mesh_bbs_ref=$1
                ;;
            --help)
                printf '%s\n' 'Usage: sh install.sh [--no-setup] [--ref TAG_OR_COMMIT] [--extras PROTOCOLS|none]'
                return 0
                ;;
            --extras)
                if [ "$#" -lt 2 ]; then
                    printf '%s\n' '--extras requires a comma-separated protocol list or none.' >&2
                    return 1
                fi
                shift
                mesh_bbs_extras=$1
                ;;
            *) printf 'Unknown option: %s\n' "$1" >&2; return 1 ;;
        esac
        shift
    done
    case "$mesh_bbs_ref" in
        ''|*[!a-zA-Z0-9._/-]*|*..*|/*|-*)
            printf '%s\n' 'Invalid source ref; use a tag, branch, or commit ID.' >&2
            return 1
            ;;
    esac

    case "$mesh_bbs_extras" in
        none) mesh_bbs_extras='' ;;
        ''|,*|*,|*,,*) printf '%s\n' 'Invalid --extras list.' >&2; return 1 ;;
        *)
            mesh_bbs_remaining=$mesh_bbs_extras
            while [ -n "$mesh_bbs_remaining" ]; do
                mesh_bbs_extra=${mesh_bbs_remaining%%,*}
                case "$mesh_bbs_extra" in
                    reticulum|meshcore|meshtastic) ;;
                    *) printf 'Unknown protocol extra: %s\n' "$mesh_bbs_extra" >&2; return 1 ;;
                esac
                case "$mesh_bbs_remaining" in
                    *,*) mesh_bbs_remaining=${mesh_bbs_remaining#*,} ;;
                    *) mesh_bbs_remaining='' ;;
                esac
            done
            ;;
    esac

    mesh_bbs_bin=${UV_TOOL_BIN_DIR:-"$HOME/.local/bin"}
    case "$mesh_bbs_bin" in
        /*) ;;
        *) printf '%s\n' 'UV_TOOL_BIN_DIR must be an absolute path.' >&2; return 1 ;;
    esac
    if [ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = x86_64 ]; then
        intel_macos_dependencies || return 1
    fi
    if command -v uv >/dev/null 2>&1; then
        mesh_bbs_uv=$(command -v uv)
    elif [ -x "$mesh_bbs_bin/uv" ]; then
        mesh_bbs_uv=$mesh_bbs_bin/uv
    else
        if ! command -v curl >/dev/null 2>&1; then
            printf '%s\n' 'Install curl with your operating system package manager, then retry.' >&2
            return 1
        fi
        mesh_bbs_tmp=$(mktemp -d "${TMPDIR:-/tmp}/mesh-bbs-install.XXXXXX")
        trap 'rm -rf "$mesh_bbs_tmp"' EXIT
        trap 'exit 1' HUP INT TERM
        curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --connect-timeout 15 --max-time 120 \
            https://astral.sh/uv/install.sh -o "$mesh_bbs_tmp/uv-install.sh"
        UV_INSTALL_DIR="$mesh_bbs_bin" UV_NO_MODIFY_PATH=1 sh "$mesh_bbs_tmp/uv-install.sh"
        mesh_bbs_uv=$mesh_bbs_bin/uv
        rm -rf "$mesh_bbs_tmp"
        trap - EXIT HUP INT TERM
    fi

    printf 'Installing Mesh BBS from %s into an isolated Python environment.\n' "$mesh_bbs_ref"
    mesh_bbs_package=mesh-bbs
    if [ -n "$mesh_bbs_extras" ]; then
        mesh_bbs_package="mesh-bbs[$mesh_bbs_extras]"
    fi
    UV_TOOL_BIN_DIR="$mesh_bbs_bin" "$mesh_bbs_uv" tool install --managed-python --python 3.12 --reinstall \
        "$mesh_bbs_package @ https://github.com/Colorado-Mesh/mesh-bbs/archive/$mesh_bbs_ref.tar.gz"
    mesh_bbs_executable=$mesh_bbs_bin/mesh-bbs
    if [ ! -x "$mesh_bbs_executable" ]; then
        printf 'Installation did not create the expected executable: %s\n' "$mesh_bbs_executable" >&2
        return 1
    fi
    printf '\nInstalled: %s\n' "$mesh_bbs_executable"
    printf '%s\n' 'Shell startup files have not been changed.'

    # The shell may be reading this script from a pipe. Setup must read the terminal.
    if [ "$mesh_bbs_setup" = 1 ] && (exec 3</dev/tty) 2>/dev/null; then
        "$mesh_bbs_executable" setup </dev/tty
    else
        printf '%s\n' 'Run setup in a terminal using this executable:'
        mesh_bbs_quoted=$(printf '%s' "$mesh_bbs_executable" | sed "s/'/'\\\\''/g")
        printf "  '%s' setup\n" "$mesh_bbs_quoted"
    fi
}

main "$@"

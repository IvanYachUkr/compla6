#!/usr/bin/env bash
# Dedicated nonroot AGENT account only; evaluator/private owner stay outside.
# Host-specific provisioning is required. See docs/PRIME_SUBSCRIPTION_RUNNER.md.
set -euo pipefail
fail() { printf '%s\n' "$*" >&2; exit 2; }
[[ $# -ge 1 ]] || fail 'Usage: prime_launch_linux.sh /absolute/public/workspace [Prime options | --command command args...]'
[[ $(id -u) -ne 0 ]] || fail 'Run as a dedicated nonroot agent, never the evaluator or root'
LAB_PUBLIC_WORKSPACE=$1; shift
: "${COMPRESSION_LAB_PRIME_ROOT:?Set the absolute commissioned Prime runtime capsule directory}"
: "${COMPRESSION_LAB_PRIME_EXECUTABLE:?Set the absolute Prime executable inside that capsule}"
: "${PRIME_AGENT_KERNEL_PYTHON:?Set the commissioned kernel Python path inside that capsule}"
: "${COMPRESSION_LAB_RELEASE_ROOT:?Set the protected installed Compression Lab directory}"
: "${COMPRESSION_LAB_NATIVE_PREFIX:?Set the pinned native library directory}"
[[ $LAB_PUBLIC_WORKSPACE = /* && $COMPRESSION_LAB_PRIME_ROOT = /* && $COMPRESSION_LAB_PRIME_EXECUTABLE = /* && $PRIME_AGENT_KERNEL_PYTHON = /* ]] || fail 'All runtime and workspace paths must be absolute'
LAB_PUBLIC_WORKSPACE=$(realpath -e -- "$LAB_PUBLIC_WORKSPACE")
LAB_PRIME_ROOT=$(realpath -e -- "$COMPRESSION_LAB_PRIME_ROOT")
LAB_AGENT_HOME=$(getent passwd "$(id -u)" | cut -d: -f6)
[[ -d $LAB_AGENT_HOME && $LAB_AGENT_HOME != / && $LAB_PRIME_ROOT != / ]] || fail 'Unsafe home or capsule path'
LAB_PROFILE=${COMPRESSION_LAB_PRIME_PROFILE:-$LAB_AGENT_HOME}
[[ $LAB_PROFILE = /* && $COMPRESSION_LAB_RELEASE_ROOT = /* && $COMPRESSION_LAB_NATIVE_PREFIX = /* ]] || fail 'Profile and dependency paths must be absolute'
LAB_PROFILE=$(realpath -e -- "$LAB_PROFILE")
LAB_RELEASE=$(realpath -e -- "$COMPRESSION_LAB_RELEASE_ROOT")
LAB_NATIVE=$(realpath -e -- "$COMPRESSION_LAB_NATIVE_PREFIX")
[[ $LAB_PROFILE != / && $LAB_RELEASE != / && $LAB_NATIVE != / ]] || fail 'Unsafe profile or dependency root'
case "$LAB_PUBLIC_WORKSPACE/" in "$LAB_PROFILE/"*) fail 'Workspace cannot live in the writable profile';; esac
case "$LAB_PROFILE/" in "$LAB_PUBLIC_WORKSPACE/"*) fail 'Profile cannot live in the public workspace';; esac
case "$LAB_PUBLIC_WORKSPACE/" in "$LAB_PRIME_ROOT/"*) fail 'Workspace cannot live in the runtime capsule';; esac
case "$LAB_PRIME_ROOT/" in "$LAB_PUBLIC_WORKSPACE/"*) fail 'Runtime capsule cannot live in the workspace';; esac
case "$COMPRESSION_LAB_PRIME_EXECUTABLE" in "$LAB_PRIME_ROOT/"*) ;; *) fail 'Prime executable must be inside the commissioned capsule';; esac
case "$PRIME_AGENT_KERNEL_PYTHON" in "$LAB_PRIME_ROOT/"*) ;; *) fail 'Kernel Python path must be inside the commissioned capsule';; esac
[[ -x $COMPRESSION_LAB_PRIME_EXECUTABLE && -x $PRIME_AGENT_KERNEL_PYTHON ]] || fail 'Missing runtime executable or kernel Python'
[[ -f $LAB_PUBLIC_WORKSPACE/dataset-card.json && -d $LAB_PUBLIC_WORKSPACE/workbench/agent && ! -L $LAB_PUBLIC_WORKSPACE/workbench/agent ]] || fail 'Not an initialized public agent workspace'
[[ -f $LAB_PROFILE/mcp-token && ! -L $LAB_PROFILE/mcp-token ]] || fail 'Provision the profile mcp-token regular file, mode 0600'
[[ $(stat -c '%u:%a' -- "$LAB_PROFILE/mcp-token") = "$(id -u):600" ]] || fail 'Service token must be owned by the agent with mode 0600'
IFS= read -r COMPRESSION_LAB_TOKEN < "$LAB_PROFILE/mcp-token"
[[ ${#COMPRESSION_LAB_TOKEN} -ge 32 ]] || fail 'Local service token must be at least 32 characters'
mkdir -p -- "$LAB_PROFILE/research-tmp"
[[ ! -L $LAB_PROFILE/research-tmp && $(realpath -e -- "$LAB_PROFILE/research-tmp") = "$LAB_PROFILE/research-tmp" ]] || fail 'Private research tmp must stay inside the profile'
chmod 700 -- "$LAB_PROFILE/research-tmp"
export COMPRESSION_LAB_TOKEN PRIME_AGENT_KERNEL_PYTHON
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
unset PYTHONPATH PYTHONHOME
LAB_MOUNT_WORKSPACE=$LAB_PUBLIC_WORKSPACE
source "$(dirname -- "${BASH_SOURCE[0]}")/researcher_mounts.sh"
command -v bwrap >/dev/null || fail 'Bubblewrap must be commissioned on this host'
if [[ ${1:-} = --command ]]; then
  shift; [[ $# -gt 0 ]] || fail 'Missing probe command'
else
  set -- "$COMPRESSION_LAB_PRIME_EXECUTABLE" "$@"
fi
# Keep network for the HOST model client and loopback MCP; candidates receive
# the evaluator's separate network-denied sandbox. This is not an agent firewall.
# Mount writable HOME first so a capsule nested in HOME stays read-only.
exec bwrap --unshare-user --unshare-pid --unshare-ipc --unshare-uts \
  --die-with-parent --new-session --cap-drop ALL \
  --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/sbin /sbin \
  --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --proc /proc --dev /dev --bind "$LAB_PROFILE/research-tmp" /tmp --tmpfs /run \
  --ro-bind /etc/passwd /etc/passwd --ro-bind /etc/group /etc/group \
  --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf \
  --ro-bind /etc/hosts /etc/hosts --ro-bind /etc/resolv.conf /etc/resolv.conf \
  --ro-bind /etc/ssl/certs /etc/ssl/certs \
  --bind "$LAB_PROFILE" "$LAB_AGENT_HOME" \
  --ro-bind "$LAB_PRIME_ROOT" "$LAB_PRIME_ROOT" \
  --ro-bind "$LAB_RELEASE" "$LAB_RELEASE" \
  --ro-bind "$LAB_NATIVE" "$LAB_NATIVE" \
  "${LAB_WORKSPACE_MOUNTS[@]}" \
  --bind "$LAB_PUBLIC_WORKSPACE/workbench/agent" "$LAB_PUBLIC_WORKSPACE/workbench/agent" \
  --chdir "$LAB_PUBLIC_WORKSPACE" \
  --setenv HOME "$LAB_AGENT_HOME" \
  --setenv PATH "$LAB_RELEASE/venv/bin:$(dirname -- "$COMPRESSION_LAB_PRIME_EXECUTABLE"):$LAB_PRIME_ROOT:/usr/bin:/bin" \
  --setenv PYTHONNOUSERSITE 1 \
  "$@"

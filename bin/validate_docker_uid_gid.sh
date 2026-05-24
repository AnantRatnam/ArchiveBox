#!/usr/bin/env bash

set -Eeuo pipefail

IMAGE="${IMAGE:-archivebox/archivebox:dev}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENTRYPOINT_PATH="${ENTRYPOINT_PATH:-$REPO_DIR/bin/docker_entrypoint.sh}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)-$$}"
VALIDATION_ROOT="${VALIDATION_ROOT:-$REPO_DIR/tmp/docker-uid-gid-validation/$RUN_ID}"
KEEP_VALIDATION_ROOT="${KEEP_VALIDATION_ROOT:-0}"

REMOTE_HOST=""
LOCAL_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --remote)
            REMOTE_HOST="$2"
            shift 2
            ;;
        --local-only)
            LOCAL_ONLY=1
            shift
            ;;
        --entrypoint)
            ENTRYPOINT_PATH="$2"
            shift 2
            ;;
        --workdir)
            VALIDATION_ROOT="$2"
            shift 2
            ;;
        *)
            echo "Usage: $0 [--remote HOST] [--local-only] [--entrypoint PATH] [--workdir PATH]" >&2
            exit 2
            ;;
    esac
done

if [[ -n "$REMOTE_HOST" && "$LOCAL_ONLY" != "1" ]]; then
    remote_dir="/tmp/archivebox-uid-gid-validation-$RUN_ID"
    ssh "$REMOTE_HOST" "mkdir -p '$remote_dir'"
    scp "$0" "$ENTRYPOINT_PATH" "$REMOTE_HOST:$remote_dir/" >/dev/null
    ssh "$REMOTE_HOST" "cd '$remote_dir' && IMAGE='$IMAGE' ENTRYPOINT_PATH='$remote_dir/$(basename "$ENTRYPOINT_PATH")' bash './$(basename "$0")' --local-only --entrypoint '$remote_dir/$(basename "$ENTRYPOINT_PATH")' --workdir '$remote_dir/work'"
    exit $?
fi

DOCKER_PLATFORM="${DOCKER_PLATFORM:-}"
if [[ -z "$DOCKER_PLATFORM" ]]; then
    case "$(uname -m)" in
        arm64|aarch64) DOCKER_PLATFORM="linux/amd64" ;;
    esac
fi

docker_base=(docker run --rm)
if [[ -n "$DOCKER_PLATFORM" ]]; then
    docker_base+=(--platform "$DOCKER_PLATFORM")
fi

mkdir -p "$VALIDATION_ROOT"

total=0
passed=0
failed=0

log() {
    printf '%s\n' "$*"
}

safe_name() {
    printf '%s' "$1" | tr -cs 'A-Za-z0-9_.-' '-'
}

docker_setup() {
    local case_dir="$1"
    local setup_script="$2"
    mkdir -p "$case_dir"
    "${docker_base[@]}" \
        -v "$case_dir:/case" \
        --entrypoint /bin/bash \
        "$IMAGE" \
        -lc "set -Eeuo pipefail
            rm -rf /case/data /case/lib /case/browsers
            mkdir -p /case/data /case/lib /case/browsers
            chown 0:0 /case/data /case/lib /case/browsers
            chmod 755 /case/data /case/lib /case/browsers
            $setup_script"
}

default_cmd='printf "ABX_UID=%s\nABX_GID=%s\nABX_USER=%s\n" "$(id -u)" "$(id -g)" "$(whoami 2>/dev/null || true)"; touch /data/logs/probe /data/archive/probe "$LIB_DIR/probe"; stat -c "ABX_STAT %u:%g:%a %n" /data /data/logs /data/archive "$LIB_DIR"; echo ABX_OK'
version_cmd='printf "ABX_UID=%s\nABX_GID=%s\nABX_USER=%s\n" "$(id -u)" "$(id -g)" "$(whoami 2>/dev/null || true)"; archivebox version >/tmp/archivebox-version.out; tail -n 12 /tmp/archivebox-version.out; echo ABX_OK'

run_case() {
    local name="$1"
    local setup_script="$2"
    local env_string="$3"
    local user_spec="$4"
    local expected_status="$5"
    local expected_uid="$6"
    local expected_gid="$7"
    local command="${8:-$default_cmd}"
    local post_assert="${9:-}"

    total=$((total + 1))
    local slug case_dir log_file status
    slug="$(safe_name "$name")"
    case_dir="$VALIDATION_ROOT/$slug"
    log_file="$case_dir/output.log"

    docker_setup "$case_dir" "$setup_script"

    local run_args=("${docker_base[@]}")
    if [[ "$user_spec" != "-" ]]; then
        run_args+=(--user "$user_spec")
    fi
    run_args+=(
        -e DATA_DIR=/data
        -e LIB_DIR=/libdir
        -e ABXPKG_LIB_DIR=/libdir
        -e PLAYWRIGHT_BROWSERS_PATH=/browsers
    )

    if [[ -n "$env_string" && "$env_string" != "-" ]]; then
        local env_parts=()
        read -r -a env_parts <<< "$env_string"
        local env_pair
        for env_pair in "${env_parts[@]}"; do
            run_args+=(-e "$env_pair")
        done
    fi

    run_args+=(
        -v "$ENTRYPOINT_PATH:/app/bin/docker_entrypoint.sh:ro"
        -v "$case_dir/data:/data"
        -v "$case_dir/lib:/libdir"
        -v "$case_dir/browsers:/browsers"
        --entrypoint /app/bin/docker_entrypoint.sh
        "$IMAGE"
        sh -c "$command"
    )

    set +e
    "${run_args[@]}" >"$log_file" 2>&1
    status=$?
    set -e

    local ok=1
    if [[ "$expected_status" == "pass" && "$status" != "0" ]]; then
        ok=0
    elif [[ "$expected_status" == "fail" && "$status" == "0" ]]; then
        ok=0
    fi

    if [[ "$expected_status" == "pass" ]]; then
        if [[ -n "$expected_uid" ]] && ! grep -q "^ABX_UID=$expected_uid$" "$log_file"; then
            ok=0
        fi
        if [[ -n "$expected_gid" ]] && ! grep -q "^ABX_GID=$expected_gid$" "$log_file"; then
            ok=0
        fi
        if ! grep -q '^ABX_OK$' "$log_file"; then
            ok=0
        fi
    fi

    if [[ "$post_assert" == "nested-root-stays" ]]; then
        local nested_stat
        nested_stat="$("${docker_base[@]}" -v "$case_dir/data:/data" --entrypoint /bin/bash "$IMAGE" -lc "stat -c '%u:%g' /data/archive/existing/file" 2>/dev/null || true)"
        [[ "$nested_stat" == "0:0" ]] || ok=0
    elif [[ "$post_assert" == users-dir-repaired ]]; then
        local users_stat
        users_stat="$("${docker_base[@]}" -v "$case_dir/data:/data" --entrypoint /bin/bash "$IMAGE" -lc "stat -c '%u:%g' /data/users" 2>/dev/null || true)"
        [[ "$users_stat" == "$expected_uid:$expected_gid" ]] || ok=0
    elif [[ "$post_assert" == config-files-repaired ]]; then
        local config_stat index_stat
        config_stat="$("${docker_base[@]}" -v "$case_dir/data:/data" --entrypoint /bin/bash "$IMAGE" -lc "stat -c '%u:%g' /data/ArchiveBox.conf" 2>/dev/null || true)"
        index_stat="$("${docker_base[@]}" -v "$case_dir/data:/data" --entrypoint /bin/bash "$IMAGE" -lc "stat -c '%u:%g' /data/index.sqlite3" 2>/dev/null || true)"
        [[ "$config_stat" == "$expected_uid:$expected_gid" && "$index_stat" == "$expected_uid:$expected_gid" ]] || ok=0
    fi

    if [[ "$ok" == "1" ]]; then
        passed=$((passed + 1))
        log "PASS $name"
    else
        failed=$((failed + 1))
        log "FAIL $name (status=$status expected=$expected_status log=$log_file)"
        sed -n '1,160p' "$log_file"
    fi
}

run_mount_case() {
    local fs_name="$1"
    local mount_dir="$2"

    if [[ -z "$mount_dir" || ! -d "$mount_dir" ]]; then
        log "SKIP $fs_name mount case: mount dir not provided"
        return
    fi
    if [[ ! -w "$mount_dir" ]]; then
        log "SKIP $fs_name mount case: $mount_dir is not writable by host user"
        return
    fi

    local previous_root case_root
    previous_root="$VALIDATION_ROOT"
    case_root="$mount_dir/archivebox-uidgid-validation-$RUN_ID"
    mkdir -p "$case_root"
    VALIDATION_ROOT="$case_root"
    run_case "$fs_name writable forced-owner style mount" ":" "PUID=911 PGID=911" "-" pass 911 911 "$default_cmd" ""
    VALIDATION_ROOT="$previous_root"
}

log "Running UID/GID validation on $(hostname) using image=$IMAGE entrypoint=$ENTRYPOINT_PATH root=$VALIDATION_ROOT platform=${DOCKER_PLATFORM:-native}"

run_case "root-owned empty data auto-detect default" \
    "chown 0:0 /case/data && chmod 755 /case/data" \
    "-" "-" pass 911 911

run_case "root-owned empty data explicit 1001:1001" \
    "chown 0:0 /case/data && chmod 755 /case/data" \
    "PUID=1001 PGID=1001" "-" pass 1001 1001

run_case "501-owned data auto-detected" \
    "chown 501:20 /case/data && chmod 755 /case/data" \
    "-" "-" pass 501 20

run_case "non-root data with root-owned config files repaired" \
    "chown 501:20 /case/data && chmod 755 /case/data && touch /case/data/index.sqlite3 /case/data/ArchiveBox.conf && mkdir -p /case/data/archive/users && chown 0:0 /case/data/index.sqlite3 /case/data/ArchiveBox.conf /case/data/archive/users" \
    "-" "-" pass 501 20 "$default_cmd" config-files-repaired

run_case "911-owned data with PGID=0 normalized" \
    "chown 911:911 /case/data && chmod 755 /case/data" \
    "PGID=0" "-" pass 911 911

run_case "911-owned data explicit 911:911" \
    "chown 911:911 /case/data && chmod 755 /case/data" \
    "PUID=911 PGID=911" "-" pass 911 911

run_case "PUID=911 PGID=0 with 501-owned writable data" \
    "chown 501:20 /case/data && chmod 777 /case/data" \
    "PUID=911 PGID=0" "-" pass 911 911

run_case "PUID=0 PGID=500 with 1001-owned writable data" \
    "chown 1001:1001 /case/data && chmod 777 /case/data" \
    "PUID=0 PGID=500" "-" pass 911 500

run_case "PUID=0 PGID=0 root-owned data normalized" \
    "chown 0:0 /case/data && chmod 755 /case/data" \
    "PUID=0 PGID=0" "-" pass 911 911

run_case "PUID=501 PGID=0 root-owned data normalized group" \
    "chown 0:0 /case/data && chmod 755 /case/data" \
    "PUID=501 PGID=0" "-" pass 501 911

run_case "PUID=0 PGID=500 root-owned data normalized user" \
    "chown 0:0 /case/data && chmod 755 /case/data" \
    "PUID=0 PGID=500" "-" pass 911 500

run_case "nested root-owned archive content is not recursively chowned" \
    "chown 0:0 /case/data && chmod 755 /case/data && mkdir -p /case/data/archive/existing && touch /case/data/archive/existing/file && chown -R 0:0 /case/data/archive/existing" \
    "PUID=1001 PGID=1002" "-" pass 1001 1002 "$default_cmd" nested-root-stays

run_case "non-root start writable root-owned data succeeds" \
    "chown 0:0 /case/data /case/lib && chmod 777 /case/data /case/lib" \
    "-" "501:911" pass 501 911

run_case "non-root start unwritable root-owned data hard-errors" \
    "chown 0:0 /case/data /case/lib && chmod 700 /case/data /case/lib" \
    "-" "501:911" fail "" ""

run_case "root start fixes read-only top-level data when chmod works" \
    "chown 0:0 /case/data && chmod 555 /case/data" \
    "PUID=911 PGID=911" "-" pass 911 911

run_case "legacy data users dir repaired when present" \
    "chown 911:911 /case/data && chmod 755 /case/data && mkdir -p /case/data/users && chown 0:0 /case/data/users" \
    "PUID=911 PGID=911" "-" pass 911 911 "$default_cmd" users-dir-repaired

run_case "archive dir root-owned inside 911 data repaired shallowly" \
    "chown 911:911 /case/data && chmod 755 /case/data && mkdir -p /case/data/archive && chown 0:0 /case/data/archive" \
    "PUID=911 PGID=911" "-" pass 911 911

run_case "logs dir root-owned mode 000 repaired shallowly" \
    "chown 911:911 /case/data && chmod 755 /case/data && mkdir -p /case/data/logs && chown 0:0 /case/data/logs && chmod 000 /case/data/logs" \
    "PUID=911 PGID=911" "-" pass 911 911

run_case "non-root user 501 can run archivebox version with root-owned LIB_DIR" \
    "chown 0:0 /case/data /case/lib && chmod 777 /case/data && chmod 755 /case/lib" \
    "-" "501:911" pass 501 911 "$version_cmd"

run_case "non-root user 501 cannot write root-owned LIB_DIR" \
    "chown 0:0 /case/data /case/lib && chmod 777 /case/data && chmod 755 /case/lib" \
    "-" "501:911" fail "" ""

run_case "non-root user 501 can write forced-owner style LIB_DIR when permissions allow" \
    "chown 0:0 /case/data /case/lib && chmod 777 /case/data /case/lib" \
    "-" "501:911" pass 501 911

run_case "1001-owned data auto-detected" \
    "chown 1001:1001 /case/data && chmod 755 /case/data" \
    "-" "-" pass 1001 1001

run_case "invalid nonnumeric PUID hard-errors" \
    "chown 911:911 /case/data && chmod 755 /case/data" \
    "PUID=abc PGID=911" "-" fail "" ""

run_case "root-owned ArchiveBox.conf only is repaired" \
    "chown 911:911 /case/data && chmod 755 /case/data && touch /case/data/ArchiveBox.conf /case/data/index.sqlite3 && chown 0:0 /case/data/ArchiveBox.conf /case/data/index.sqlite3" \
    "PUID=911 PGID=911" "-" pass 911 911 "$default_cmd" config-files-repaired

run_mount_case "NFS" "${NFS_TEST_DIR:-}"
run_mount_case "SMB" "${SMB_TEST_DIR:-}"

log "SUMMARY passed=$passed failed=$failed total=$total"

if [[ "$KEEP_VALIDATION_ROOT" != "1" ]]; then
    rm -rf "$VALIDATION_ROOT"
fi

[[ "$failed" == "0" ]]

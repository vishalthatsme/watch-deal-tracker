#!/bin/zsh
set -euo pipefail
umask 077

script_directory="${0:A:h}"
project_root="${script_directory:h}"
user_home="${HOME:A}"
if [[ ! -d "${user_home}" || "${user_home}" != /* ]]; then
  echo "Could not resolve a safe absolute user home directory" >&2
  exit 1
fi
runtime_root="${user_home}/Library/Application Support/WatchTracker"
launch_agents_root="${user_home}/Library/LaunchAgents"
launchd_label="io.github.vishalthatsme.watch-deal-tracker"
launchd_plist="${launch_agents_root}/${launchd_label}.plist"
plist_template="${project_root}/operations/launchd/watch-deal-tracker.plist.template"
uv_binary="$(command -v uv || true)"
if [[ -z "${uv_binary}" || ! -x "${uv_binary}" ]]; then
  echo "uv is required on PATH; install it from https://docs.astral.sh/uv/" >&2
  exit 1
fi
deployment_cache="/private/tmp/watch-tracker-$(id -u)-deploy-cache"
user_domain="gui/$(id -u)"
staging_root=""
build_root=""
candidate_link=""
generated_plist=""
service_was_loaded=false
deployment_committed=false

cleanup() {
  if [[ -n "${candidate_link}" && -L "${candidate_link}" ]]; then
    /bin/rm -f "${candidate_link}"
  fi
  if [[
    -n "${staging_root}" &&
    -d "${staging_root}" &&
    -f "${staging_root}/.incomplete" &&
    "${staging_root}" == "${runtime_root}/releases/"*
  ]]; then
    /bin/rm -rf "${staging_root}"
  fi
  if [[
    -n "${build_root}" &&
    -d "${build_root}" &&
    "${build_root}" == "${deployment_cache}/build."*
  ]]; then
    /bin/rm -rf "${build_root}"
  fi
  if [[
    "${service_was_loaded}" == "true" &&
    "${deployment_committed}" != "true" &&
    -f "${launchd_plist}"
  ]]; then
    /bin/launchctl bootstrap "${user_domain}" "${launchd_plist}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT HUP INT TERM

mkdir -p \
  "${runtime_root}/config" \
  "${runtime_root}/data/database" \
  "${runtime_root}/data/exports" \
  "${runtime_root}/data/backups" \
  "${runtime_root}/data/evidence" \
  "${runtime_root}/logs" \
  "${runtime_root}/migrations/versions" \
  "${runtime_root}/releases" \
  "${launch_agents_root}"
mkdir -p "${deployment_cache}"

chmod 700 \
  "${runtime_root}" \
  "${runtime_root}/config" \
  "${runtime_root}/data" \
  "${runtime_root}/data/database" \
  "${runtime_root}/data/exports" \
  "${runtime_root}/data/backups" \
  "${runtime_root}/data/evidence" \
  "${runtime_root}/logs" \
  "${runtime_root}/migrations" \
  "${runtime_root}/migrations/versions" \
  "${runtime_root}/releases" \
  "${deployment_cache}"

exec 8>"${runtime_root}/deploy.lock"
chmod 600 "${runtime_root}/deploy.lock"
if ! /usr/bin/lockf -s -t 60 8; then
  echo "Timed out waiting for the watch-tracker deployment lock" >&2
  exit 75
fi

if [[ ! -f "${project_root}/uv.lock" ]]; then
  echo "Refusing deployment without ${project_root}/uv.lock" >&2
  exit 1
fi

build_root="$(/usr/bin/mktemp -d "${deployment_cache}/build.XXXXXX")"
UV_CACHE_DIR="${deployment_cache}" \
  UV_PYTHON_INSTALL_DIR="${project_root}/.python" \
    "${uv_binary}" build --wheel --out-dir "${build_root}"

wheel_paths=("${build_root}"/*.whl(N))
if (( ${#wheel_paths} != 1 )); then
  echo "Expected exactly one wheel under ${build_root}" >&2
  exit 1
fi
wheel_path="${wheel_paths[1]}"
wheel_digest="$(/usr/bin/shasum -a 256 "${wheel_path}" | /usr/bin/awk '{print $1}')"
lock_digest="$(/usr/bin/shasum -a 256 "${project_root}/uv.lock" | /usr/bin/awk '{print $1}')"
release_hash="$(
  /usr/bin/printf '%s\n%s\n' "${wheel_digest}" "${lock_digest}" |
    /usr/bin/shasum -a 256 |
    /usr/bin/awk '{print $1}'
)"
release_root="${runtime_root}/releases/${release_hash}"

validate_release() {
  local candidate="$1"
  local allow_incomplete="${2:-false}"
  if [[ ! -d "${candidate}" || -L "${candidate}" ]]; then
    echo "Release is not a real directory: ${candidate}" >&2
    return 1
  fi
  if [[ -f "${candidate}/.incomplete" && "${allow_incomplete}" != "true" ]]; then
    echo "Release is marked incomplete: ${candidate}" >&2
    return 1
  fi
  if [[ ! -f "${candidate}/.release-id" ]]; then
    echo "Release manifest is missing: ${candidate}/.release-id" >&2
    return 1
  fi
  if [[ "$(<"${candidate}/.release-id")" != "${release_hash}" ]]; then
    echo "Release manifest does not match its content identity: ${candidate}" >&2
    return 1
  fi
  if [[ ! -x "${candidate}/venv/bin/watch-tracker" ]]; then
    echo "Release entrypoint is missing: ${candidate}" >&2
    return 1
  fi
  UV_CACHE_DIR="${deployment_cache}" \
    "${uv_binary}" pip check --python "${candidate}/venv/bin/python"
  PYTHONDONTWRITEBYTECODE=1 \
    "${candidate}/venv/bin/watch-tracker" --help >/dev/null
}

if [[ -e "${release_root}" || -L "${release_root}" ]]; then
  if ! validate_release "${release_root}"; then
    current_target=""
    if [[ -L "${runtime_root}/current" ]]; then
      current_target="$(/usr/bin/readlink "${runtime_root}/current")"
    fi
    if [[ "${current_target}" == "${release_root}" ]]; then
      echo "Current release is invalid; refusing to quarantine the active target" >&2
      exit 1
    fi
    invalid_release="${runtime_root}/releases/.invalid-${release_hash}-$(
      /bin/date -u +%Y%m%dT%H%M%SZ
    )"
    /bin/mv "${release_root}" "${invalid_release}"
    echo "Quarantined invalid inactive release at ${invalid_release}" >&2
  fi
fi

if [[ ! -e "${release_root}" && ! -L "${release_root}" ]]; then
  # Virtualenv console scripts contain absolute interpreter paths, so the
  # candidate must be built at its final path. It is never made current until
  # validation succeeds; .incomplete makes interrupted candidates disposable.
  mkdir "${release_root}"
  chmod 700 "${release_root}"
  staging_root="${release_root}"
  : >"${staging_root}/.incomplete"
  chmod 600 "${staging_root}/.incomplete"

  UV_CACHE_DIR="${deployment_cache}" \
    "${uv_binary}" export \
      --quiet \
      --directory "${project_root}" \
      --locked \
      --no-dev \
      --no-emit-project \
      --format requirements.txt \
      --output-file "${staging_root}/requirements.lock.txt"

  UV_CACHE_DIR="${deployment_cache}" \
  UV_PYTHON_INSTALL_DIR="${runtime_root}/python" \
    "${uv_binary}" venv "${staging_root}/venv" --python 3.12

  UV_CACHE_DIR="${deployment_cache}" \
    "${uv_binary}" pip install \
      --python "${staging_root}/venv/bin/python" \
      --requirements "${staging_root}/requirements.lock.txt" \
      --require-hashes \
      --strict

  UV_CACHE_DIR="${deployment_cache}" \
    "${uv_binary}" pip install \
      --python "${staging_root}/venv/bin/python" \
      --no-deps \
      "${wheel_path}"

  /usr/bin/printf '%s\n' "${release_hash}" >"${staging_root}/.release-id"
  /usr/bin/printf 'wheel_sha256=%s\nuv_lock_sha256=%s\n' \
    "${wheel_digest}" \
    "${lock_digest}" \
    >"${staging_root}/release-manifest.txt"
  chmod 600 \
    "${staging_root}/.release-id" \
    "${staging_root}/release-manifest.txt" \
    "${staging_root}/requirements.lock.txt"

  validate_release "${staging_root}" true
  /bin/rm -f "${staging_root}/.incomplete"
  validate_release "${release_root}"
  staging_root=""
fi

if [[ ! -f "${plist_template}" ]]; then
  echo "LaunchAgent template is missing: ${plist_template}" >&2
  exit 1
fi
generated_plist="${build_root}/${launchd_label}.plist"
install -m 600 "${plist_template}" "${generated_plist}"
/usr/bin/plutil -replace Label -string "${launchd_label}" "${generated_plist}"
/usr/bin/plutil -replace ProgramArguments.0 \
  -string "${runtime_root}/current/venv/bin/watch-tracker" \
  "${generated_plist}"
/usr/bin/plutil -replace ProgramArguments.3 \
  -string "${runtime_root}/config/default.yaml" \
  "${generated_plist}"
/usr/bin/plutil -replace WorkingDirectory -string "${runtime_root}" "${generated_plist}"
/usr/bin/plutil -replace EnvironmentVariables.PATH \
  -string "${runtime_root}/current/venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  "${generated_plist}"
/usr/bin/plutil -lint "${generated_plist}"

if /bin/launchctl print "${user_domain}/${launchd_label}" >/dev/null 2>&1; then
  service_was_loaded=true
  /bin/launchctl bootout "${user_domain}/${launchd_label}"
fi

exec 9>"${runtime_root}/data/database/watch_tracker.lock"
chmod 600 "${runtime_root}/data/database/watch_tracker.lock"
if ! /usr/bin/lockf -s -t 60 9; then
  echo "Timed out waiting for the watch-tracker application lock" >&2
  exit 75
fi

refresh_runtime_config=false
if [[ ! -f "${runtime_root}/config/default.yaml" ]]; then
  refresh_runtime_config=true
elif [[ -f "${runtime_root}/config/default.distributed.yaml" ]] && \
  cmp -s \
    "${runtime_root}/config/default.yaml" \
    "${runtime_root}/config/default.distributed.yaml"; then
  refresh_runtime_config=true
fi
install -m 600 \
  "${project_root}/config/default.yaml" \
  "${runtime_root}/config/default.distributed.yaml"
if [[ "${refresh_runtime_config}" == "true" ]]; then
  install -m 600 \
    "${project_root}/config/default.yaml" \
    "${runtime_root}/config/default.yaml"
fi
install -m 600 \
  "${project_root}/config/secrets.env.example" \
  "${runtime_root}/config/secrets.env.example"
install -m 600 "${project_root}/alembic.ini" "${runtime_root}/alembic.ini"
ditto "${project_root}/migrations" "${runtime_root}/migrations"
/usr/bin/find "${runtime_root}/migrations" -type d -exec chmod 700 {} +
/usr/bin/find "${runtime_root}/migrations" -type f -exec chmod 600 {} +

WATCH_TRACKER_DEPLOYMENT_LOCK_HELD=1 \
  "${release_root}/venv/bin/watch-tracker" migrate \
  --config "${runtime_root}/config/default.yaml"

if [[ -f "${runtime_root}/data/database/watch_market.sqlite" ]]; then
  chmod 600 "${runtime_root}/data/database/watch_market.sqlite"
fi
/usr/bin/find \
  "${runtime_root}/data/backups" \
  "${runtime_root}/data/exports" \
  "${runtime_root}/data/evidence" \
  "${runtime_root}/logs" \
  -type f -exec chmod 600 {} +

candidate_link="${runtime_root}/.current-${release_hash}-$$"
/bin/ln -s "${release_root}" "${candidate_link}"
if [[ -e "${runtime_root}/current" && ! -L "${runtime_root}/current" ]]; then
  echo "${runtime_root}/current exists and is not a symlink; refusing replacement" >&2
  exit 1
fi
/bin/mv -fh "${candidate_link}" "${runtime_root}/current"
exec 9>&-

install -m 600 \
  "${generated_plist}" \
  "${launchd_plist}"
/usr/bin/plutil -lint "${launchd_plist}"

/bin/launchctl enable "${user_domain}/${launchd_label}"
/bin/launchctl bootstrap "${user_domain}" "${launchd_plist}"
deployment_committed=true
/bin/launchctl print "${user_domain}/${launchd_label}"

echo "Installed ${launchd_label} release ${release_hash} from ${launchd_plist}"

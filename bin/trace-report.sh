#!/usr/bin/env bash
# RHEL 9: Bash is the entry point; Python's standard library fills the XLSX template.
set -Eeuo pipefail
umask 077

usage() {
  cat <<'USAGE'
Usage: bash bin/trace-report.sh --start TIME --end TIME --region REGION --output-dir DIR [OPTIONS]

Query aws/spans with CloudWatch Logs Insights and export CSV + an Excel trace report.
TIME: RFC3339 with timezone (millisecond precision) or integer Unix seconds.
The selected log-event timestamp interval is [start, end), in UTC.

  --start TIME           Inclusive start, e.g. 2026-10-26T09:00:00+09:00
  --end TIME             Exclusive end, e.g. 2026-10-26T10:00:00+09:00
  --region REGION        AWS region (required, including offline mode)
  --output-dir DIR       New output directory; an existing path is never overwritten
  --profile NAME         AWS CLI profile (otherwise use the normal credential chain)
  --switchback-mode MODE warn: advise and exit on AWS auth errors (default); auto: source and retry
  --switchback-script FILE  Team-provided Bash shell to source; required for live auto mode
  --log-group NAME       Default: aws/spans
  --trace-id ID          Optional 32-hex or X-Ray trace ID; includes both ID formats
  --template FILE        Compatible XLSX template; default: templates/trace-report.xlsx
  --query-limit N        Events per query, 2..100000 (default: 10000)
  --poll-interval N      Seconds between polls, 1..60 (default: 2)
  --timeout N            Deadline per query in seconds (default: 900)
  --max-queries N        Maximum number of queries, including split queries (default: 1000)
  --from-results FILE    Offline: one complete, untruncated GetQueryResults JSON response
  --help                 Show this message

Runtime: Bash >= 4.4, GNU coreutils, jq >= 1.6, Python >= 3.9 (no pip packages).
Live mode also needs AWS CLI v2 and logs:StartQuery/GetQueryResults/StopQuery.
USAGE
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '%s\n' "$*" >&2; }
need_value() { [[ $# -ge 2 && -n $2 ]] || die "Missing value for $1"; }
integer_option() {
  local name=$1 value=$2 minimum=$3 maximum=$4
  [[ $value =~ ^[1-9][0-9]{0,8}$ ]] || die "$name must be an integer"
  (( value >= minimum && value <= maximum )) || die "$name must be $minimum..$maximum"
}

(( BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4) )) || die 'Bash >= 4.4 is required'
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
project_dir=$(cd -- "$script_dir/.." && pwd -P)
helper="$project_dir/lib/report.py"
template="$project_dir/templates/trace-report.xlsx"
start='' end='' region='' output_dir='' profile='' trace_id='' from_results=''
switchback_mode=warn switchback_script='' switchback_attempted=0
log_group='aws/spans'
query_limit=10000 poll_interval=2 timeout=900 max_queries=1000

while (( $# )); do
  case $1 in
    --start) need_value "$@"; start=$2; shift 2 ;;
    --end) need_value "$@"; end=$2; shift 2 ;;
    --region) need_value "$@"; region=$2; shift 2 ;;
    --output-dir) need_value "$@"; output_dir=$2; shift 2 ;;
    --profile) need_value "$@"; profile=$2; shift 2 ;;
    --switchback-mode) need_value "$@"; switchback_mode=$2; shift 2 ;;
    --switchback-script) need_value "$@"; switchback_script=$2; shift 2 ;;
    --log-group) need_value "$@"; log_group=$2; shift 2 ;;
    --trace-id) need_value "$@"; trace_id=$2; shift 2 ;;
    --template) need_value "$@"; template=$2; shift 2 ;;
    --query-limit) need_value "$@"; query_limit=$2; shift 2 ;;
    --poll-interval) need_value "$@"; poll_interval=$2; shift 2 ;;
    --timeout) need_value "$@"; timeout=$2; shift 2 ;;
    --max-queries) need_value "$@"; max_queries=$2; shift 2 ;;
    --from-results) need_value "$@"; from_results=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) die "Unknown argument: $1 (use --help)" ;;
  esac
done

[[ -n $start && -n $end && -n $region && -n $output_dir ]] || die 'Specify --start, --end, --region and --output-dir'
[[ $region =~ ^[a-z0-9]+(-[a-z0-9]+)+-[0-9]+$ ]] || die 'Invalid region'
[[ $log_group =~ ^[.A-Za-z0-9_/#-]+$ && ${#log_group} -le 512 ]] || die 'Invalid log group name'
integer_option --query-limit "$query_limit" 2 100000
integer_option --poll-interval "$poll_interval" 1 60
integer_option --timeout "$timeout" 1 86400
integer_option --max-queries "$max_queries" 1 1000000
case $switchback_mode in
  warn|auto) ;;
  *) die '--switchback-mode must be warn or auto' ;;
esac
if [[ -z $from_results && $switchback_mode == auto && -z $switchback_script ]]; then
  die '--switchback-mode auto requires --switchback-script FILE'
fi
for dependency in python3 jq realpath mktemp mv; do
  command -v "$dependency" >/dev/null || die "Required command not found: $dependency"
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die 'Python >= 3.9 is required'
[[ -r $helper && -r $template ]] || die 'Report helper or XLSX template is missing'
python3 "$helper" validate-template "$template"
time_range=$(python3 "$helper" time-range "$start" "$end")
read -r start_ms end_ms <<< "$time_range"
if [[ -n $trace_id ]]; then
  trace_id=$(python3 "$helper" trace-id "$trace_id")
fi
if [[ -n $from_results ]]; then
  [[ -r $from_results ]] || die 'Cannot read --from-results file'
else
  if [[ -n $switchback_script ]]; then
    [[ -f $switchback_script && -r $switchback_script ]] || die '--switchback-script must be a readable file'
    switchback_script=$(realpath -e -- "$switchback_script")
  fi
  command -v aws >/dev/null || die 'AWS CLI v2 is required'
  aws_version=$(aws --version 2>&1)
  [[ $aws_version == aws-cli/2.* ]] || die 'AWS CLI v2 is required'
fi

output_dir=$(realpath -m -- "$output_dir")
[[ ! -e $output_dir && ! -L $output_dir ]] || die 'Output path already exists; choose a new --output-dir'
output_parent=$(dirname -- "$output_dir")
mkdir -p -- "$output_parent"
work_dir=$(mktemp -d "$output_parent/.trace-report.XXXXXXXX")
active_query=''
aws_args=(--region "$region" --output json --no-cli-pager --no-paginate --cli-connect-timeout 10 --cli-read-timeout 60)
[[ -z $profile ]] || aws_args+=(--profile "$profile")
export AWS_PAGER='' AWS_CLI_AUTO_PROMPT=off
export AWS_RETRY_MODE="${AWS_RETRY_MODE:-standard}" AWS_MAX_ATTEMPTS="${AWS_MAX_ATTEMPTS:-5}"

aws_auth_error() {
  local message pattern
  message=$(< "$1")
  # A CLI exit code alone also covers service/configuration errors unrelated to IAM.
  pattern='An error occurred \((AccessDenied(Exception)?|UnauthorizedOperation|UnauthorizedException|ForbiddenException|UnrecognizedClientException|InvalidClientTokenId|InvalidAccessKeyId|ExpiredToken(Exception)?|SignatureDoesNotMatch|InvalidSignatureException)\)'
  [[ $message =~ $pattern || $message == *'Unable to locate credentials'* ||
     $message == *'Partial credentials found'* || $message == *'Token has expired and refresh failed'* ||
     $message == *'The SSO session associated with this profile has expired or is otherwise invalid'* ]]
}

perform_switchback() {
  local switchback_status=0 switchback_directory=$PWD
  switchback_attempted=1
  log 'AWS authentication/authorization failed; sourcing --switchback-script for switchback'
  # Source in this process, with no positional arguments, so exported credentials
  # persist for the retry, later queries and cleanup. Script output is not API JSON.
  # shellcheck disable=SC1090
  source "$switchback_script" >&2 || switchback_status=$?
  set -Eeuo pipefail
  umask 077
  cd -- "$switchback_directory" || die 'Could not restore working directory after switchback'
  if (( switchback_status != 0 )); then
    log "ERROR: Switchback script failed (exit $switchback_status); AWS operation was not retried"
    return "$switchback_status"
  fi
  (( SECONDS < deadline )) || die 'Switchback exceeded the query --timeout'
  log 'Retrying the failed AWS operation after switchback'
}

aws_call() {
  local destination=$1 aws_status error_file="$work_dir/aws-error.txt"
  shift
  while :; do
    # Overwrite failed stdout on retry rather than appending it to a valid response.
    if aws "${aws_args[@]}" "$@" > "$destination" 2> "$error_file"; then
      cat -- "$error_file" >&2
      return 0
    else
      aws_status=$?
    fi
    cat -- "$error_file" >&2
    aws_auth_error "$error_file" || return "$aws_status"
    if [[ $switchback_mode == warn ]]; then
      log 'WARNING: AWS authentication/authorization failed. Switchback is required before continuing.'
      if [[ -n $switchback_script ]]; then
        printf 'WARNING: Run: source %q; then rerun this command.\n' "$switchback_script" >&2
      else
        log 'WARNING: Source your team-provided switchback shell, then rerun this command.'
      fi
      log 'WARNING: For automatic switchback, use --switchback-mode auto --switchback-script FILE.'
      return "$aws_status"
    fi
    if (( switchback_attempted )); then
      log 'ERROR: AWS authentication/authorization still fails after switchback; no further switchback will be attempted.'
      return "$aws_status"
    fi
    perform_switchback
  done
}

cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n $active_query ]]; then
    # Cleanup also runs on cancellation: do not start a new switchback while exiting.
    if ! aws "${aws_args[@]}" logs stop-query --query-id "$active_query" >/dev/null; then
      log "WARNING: Could not stop query $active_query; check it in CloudWatch Logs Insights"
    fi
  fi
  # Only remove the directory returned by mktemp in the explicitly chosen parent.
  if [[ -n $work_dir && -d $work_dir && $work_dir == "$output_parent"/.trace-report.* ]]; then
    rm -rf -- "$work_dir"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -- "$work_dir/result"
events_file="$work_dir/result/raw-results.jsonl"
queries_file="$work_dir/queries.jsonl"
: > "$events_file"
: > "$queries_file"
query_count=0

append_results() {
  jq -c '.results[] | map({key: .field, value: .value}) | from_entries' "$1" >> "$2"
}

if [[ -n $from_results ]]; then
  # Refuse partial snapshots; offline mode cannot recover their missing pages.
  jq -e --argjson cap "$query_limit" '
    .status == "Complete" and (.results | type == "array") and
    ((.nextToken // "") == "") and (.results | length) < $cap and
    ((.statistics.recordsMatched // (.results | length)) <= (.results | length))
  ' "$from_results" >/dev/null || die 'Offline response must be Complete, unpaginated and below --query-limit'
  append_results "$from_results" "$events_file"
  log 'Using offline GetQueryResults response'
else
  # Each stack item is a half-open interval in milliseconds. API bounds are rounded
  # outwards to seconds; the QL predicate provides exact, non-overlapping boundaries.
  stack_start=("$start_ms") stack_end=("$end_ms")
  while (( ${#stack_start[@]} )); do
    last=$(( ${#stack_start[@]} - 1 ))
    lo=${stack_start[$last]} hi=${stack_end[$last]}
    unset 'stack_start[last]' 'stack_end[last]'
    (( query_count < max_queries )) || die 'Maximum query count reached; narrow the interval or raise --max-queries'
    query_count=$(( query_count + 1 ))
    query="fields @timestamp, @message, @ptr | filter toMillis(@timestamp) >= $lo and toMillis(@timestamp) < $hi"
    if [[ -n $trace_id ]]; then
      xray_trace_id="1-${trace_id:0:8}-${trace_id:8}"
      query+=" | filter traceId = '$trace_id' or traceId = '$xray_trace_id'"
    fi
    query+=' | sort @timestamp asc'
    log "Query $query_count: [$lo, $hi) epoch milliseconds"
    deadline=$(( SECONDS + timeout ))
    aws_call "$work_dir/start.json" logs start-query --log-group-name "$log_group" \
      --start-time "$(( lo / 1000 ))" --end-time "$(( (hi + 999) / 1000 ))" \
      --limit "$query_limit" --query-string "$query"
    active_query=$(jq -er '.queryId | select(type == "string" and length > 0)' "$work_dir/start.json")
    response="$work_dir/page.json"
    while :; do
      (( SECONDS < deadline )) || die "Query $active_query exceeded --timeout"
      aws_call "$response" logs get-query-results --query-id "$active_query"
      status=$(jq -er '.status | select(type == "string")' "$response")
      case $status in
        Complete) break ;;
        Scheduled|Running)
          (( SECONDS < deadline )) || die "Query $active_query exceeded --timeout"
          sleep "$poll_interval"
          ;;
        *) die "Query $active_query ended with status $status" ;;
      esac
    done
    completed_query=$active_query
    active_query=''
    cp -- "$response" "$work_dir/complete.json"
    slice_events="$work_dir/slice.jsonl"
    : > "$slice_events"
    rows=0 pages=0
    printf '[]\n' > "$work_dir/tokens.json"
    while :; do
      jq -e '.status == "Complete" and (.results | type == "array")' "$response" >/dev/null || die 'Invalid result page'
      page_rows=$(jq '.results | length' "$response")
      rows=$(( rows + page_rows ))
      pages=$(( pages + 1 ))
      (( pages <= 1000 )) || die 'Too many result pages'
      append_results "$response" "$slice_events"
      token=$(jq -er '.nextToken // "" | if type == "string" then . else error("Invalid nextToken") end' "$response")
      [[ -n $token ]] || break
      jq -e --arg token "$token" 'index($token) == null' "$work_dir/tokens.json" >/dev/null || die 'Repeated pagination token from GetQueryResults'
      jq --arg token "$token" '. + [$token]' "$work_dir/tokens.json" > "$work_dir/tokens-next.json"
      mv -- "$work_dir/tokens-next.json" "$work_dir/tokens.json"
      (( SECONDS < deadline )) || die "Result pagination for $completed_query exceeded --timeout"
      # JSON input keeps opaque tokens out of shell interpolation and supports the
      # API nextToken separately from the AWS CLI's own pagination switches.
      request=$(jq -cn --arg id "$completed_query" --arg token "$token" '{queryId: $id, nextToken: $token}')
      aws_call "$response" logs get-query-results --cli-input-json "$request"
    done
    split=$(jq -r --argjson rows "$rows" --argjson cap "$query_limit" \
      '$rows >= $cap or ((.statistics.recordsMatched // $rows) > $rows)' "$work_dir/complete.json")
    jq -c --arg id "$completed_query" --arg query "$query" \
      --argjson lo "$lo" --argjson hi "$hi" --argjson rows "$rows" \
      --argjson pages "$pages" --argjson split "$split" \
      '{query_id: $id, start_ms: $lo, end_ms: $hi, query: $query,
        rows: $rows, pages: $pages, split: $split, statistics: (.statistics // {})}' \
      "$work_dir/complete.json" >> "$queries_file"
    if [[ $split == true ]]; then
      (( hi - lo > 1 )) || die 'Query limit reached within one millisecond; increase --query-limit or narrow with --trace-id. No partial report was exported.'
      mid=$(( lo + (hi - lo) / 2 ))
      log 'Result limit reached; splitting the interval and discarding its truncated result'
      stack_start+=("$mid" "$lo")
      stack_end+=("$hi" "$mid")
    else
      cat -- "$slice_events" >> "$events_file"
    fi
  done
fi

python3 "$helper" render --events "$events_file" --queries "$queries_file" \
  --template "$template" --output-dir "$work_dir/result" \
  --start-ms "$start_ms" --end-ms "$end_ms" --region "$region" \
  --log-group "$log_group" --trace-id "$trace_id" \
  --source "$([[ -n $from_results ]] && printf offline || printf cloudwatch)"

# GNU mv -n also protects against a target created after preflight. Check whether
# it actually moved: coreutils may return success when --no-clobber skips a file.
mv -T -n -- "$work_dir/result" "$output_dir"
[[ ! -d $work_dir/result ]] || die 'Output path was created concurrently; nothing was overwritten'
log "Created: $output_dir/report.xlsx"
log "CSV:     $output_dir/spans.csv"

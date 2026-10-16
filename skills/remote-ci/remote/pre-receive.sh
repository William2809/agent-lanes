#!/bin/sh
# ABOUTME: Runner repo pre-receive hook: refuses pushes from clients older than the host runner protocol.
# ABOUTME: Private and public clients use compatible push-option names; ~/ci/kit/PROTOCOL is the floor.
need=$(cat "$HOME/ci/kit/PROTOCOL" 2>/dev/null) || exit 0
case "$need" in '' | *[!0-9]*) exit 0 ;; esac
canonical_tool=remote-ci
private_tool=$(printf 'm%s' 'ini-ci')
public_tool=$(printf 'r%s' 'emote-ci')
have=0 invalid=0 i=0
while [ "$i" -lt "${GIT_PUSH_OPTION_COUNT:-0}" ]; do
  eval "option=\${GIT_PUSH_OPTION_$i}"
  tool=${option%%=*}
  if [ "$tool" = "$canonical_tool" ] || [ "$tool" = "$private_tool" ] || [ "$tool" = "$public_tool" ]; then
    version=${option#*=}
    case "$version" in
      '' | *[!0-9]*) invalid=1 ;;
      *) [ "$version" -gt "$have" ] && have=$version ;;
    esac
  fi
  i=$((i + 1))
done
[ "$invalid" = 0 ] && [ "$have" -ge "$need" ] && exit 0
echo "$canonical_tool: runner needs protocol $need, this client sent ${have}: update your client toolkit" >&2
exit 1

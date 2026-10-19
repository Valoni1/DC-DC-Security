#!/usr/bin/env bash
# diagnose_srl_boot.sh
#
# Finds out why SR Linux containers stop at boot with
#   "sudo: account validation failure, is your account locked?"
# and, with --fix, applies the fix and proves it.
#
# It collects everything in one go and writes it to srl_diag.txt:
#   - host facts: date, uptime, kernel, Docker, containerlab, AppArmor/SELinux
#   - package changes since 1 Oct (apt history, unattended-upgrades logs)
#   - AppArmor denials in the kernel log
#   - sudo inside the SR Linux image as user and as real root, privileged and
#     not privileged, plus the pam_unix helper (unix_chkpwd) called directly
#   - a verdict naming the cause
#
# USAGE (on the lab host, from the repo folder):
#   sudo bash tools/host/diagnose_srl_boot.sh          # diagnose only, changes nothing
#   sudo bash tools/host/diagnose_srl_boot.sh --fix    # diagnose, fix if the cause is
#                                           # the AppArmor unix-chkpwd profile,
#                                           # then re-test
# Then paste srl_diag.txt (or its VERDICT section) back.

IMAGE="${SRL_IMAGE:-ghcr.io/nokia/srlinux:26.7.2}"
OUT="$(dirname "$(readlink -f "$0")")/srl_diag.txt"
SINCE="2026-10-01"
FIX=0
[ "$1" = "--fix" ] && FIX=1

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash $0 $*"
    exit 1
fi

exec > >(tee "$OUT") 2>&1

section() { printf '\n===== %s =====\n' "$1"; }

# One sudo test in a throwaway container. Prints PASS/FAIL and sudo's output.
#   $1 label  $2 docker user  $3 "priv" or "nopriv"
sudo_test() {
    local label="$1" user="$2" priv=()
    [ "$3" = "priv" ] && priv=(--privileged)
    local out rc
    out=$(timeout 60 docker run --rm "${priv[@]}" --user "$user" --entrypoint bash "$IMAGE" \
        -c 'sudo -n true 2>&1; echo "rc=$?"' 2>&1)
    rc=$(printf '%s\n' "$out" | sed -n 's/^rc=//p' | tail -1)
    if [ "$rc" = "0" ]; then
        printf '%-34s PASS\n' "$label"
    else
        printf '%-34s FAIL  %s\n' "$label" "$(printf '%s' "$out" | grep -v '^rc=' | tr '\n' ' ')"
    fi
    [ "$rc" = "0" ]
}

section "HOST"
date; date -u
uptime
uname -a
grep PRETTY_NAME /etc/os-release
docker version --format 'docker client {{.Client.Version}}  server {{.Server.Version}}' 2>&1
docker info --format 'storage={{.Driver}} security={{.SecurityOptions}}' 2>&1
containerlab version 2>/dev/null | grep -E 'version|date'
docker image inspect "$IMAGE" --format 'image {{.Id}} created {{.Created}} user={{.Config.User}}' 2>&1

section "APPARMOR / SELINUX ON HOST"
if command -v aa-status >/dev/null; then
    aa-status 2>&1 | head -3
    echo "chkpwd-related profiles loaded:"
    aa-status 2>/dev/null | grep -iE 'chkpwd|sudo|unix' || echo "  none"
else
    echo "aa-status not installed"
fi
echo "profile files:"
ls -l /etc/apparmor.d/ 2>/dev/null | grep -iE 'chkpwd|sudo|unix' || echo "  none"
echo "disabled profiles:"
ls /etc/apparmor.d/disable/ 2>/dev/null || echo "  none"
[ -d /sys/fs/selinux ] && echo "selinuxfs mounted on host" || echo "selinuxfs not mounted"

section "PACKAGE CHANGES SINCE $SINCE"
for f in /var/log/apt/history.log /var/log/apt/history.log.1; do
    [ -f "$f" ] || continue
    awk -v since="$SINCE" '
        /^Start-Date:/ { keep = ($2 >= since) }
        keep && /^(Start-Date|Commandline|Requested-By|Install|Upgrade|Remove):/ { print substr($0, 1, 600) }
    ' "$f"
done
echo "-- zipped history mentioning apparmor/pam/sudo/docker:"
zgrep -hE '^(Start-Date|Upgrade|Install).*(apparmor|libpam|sudo|docker|containerd|linux-image)' \
    /var/log/apt/history.log.*.gz 2>/dev/null | tail -10 || true
echo "-- unattended-upgrades since $SINCE:"
for f in /var/log/unattended-upgrades/unattended-upgrades.log /var/log/unattended-upgrades/unattended-upgrades.log.1; do
    [ -f "$f" ] || continue
    awk -v since="$SINCE" '$1 >= since && /Packages that will be upgraded|All upgrades installed|ERROR/' "$f" | cut -c1-600
done
echo "-- dpkg changes to apparmor/pam/sudo/docker since $SINCE:"
cat /var/log/dpkg.log /var/log/dpkg.log.1 2>/dev/null \
    | awk -v since="$SINCE" '$1 >= since && $3 ~ /^(upgrade|install)$/' \
    | grep -E 'apparmor|libpam|sudo|docker|containerd|runc|linux-image' | tail -20

section "APPARMOR DENIALS BEFORE TESTS (last 10)"
dmesg 2>/dev/null | grep -i 'apparmor="DENIED"' | tail -10 || echo "none"
DENIED_BEFORE=$(dmesg 2>/dev/null | grep -ci 'apparmor="DENIED"')

section "INSIDE THE IMAGE"
timeout 60 docker run --rm --privileged --user 0:0 --entrypoint bash "$IMAGE" -c '
    echo "apparmor label of this shell: $(cat /proc/self/attr/current 2>/dev/null)"
    ls -l /usr/bin/sudo /usr/sbin/unix_chkpwd /etc/shadow /etc/passwd 2>&1
    [ -d /sys/fs/selinux ] && echo "selinuxfs visible in container" || echo "selinuxfs not visible"
    grep -E "^(root|user|linuxadmin|srlinux):" /etc/shadow
    echo "-- sudoers (non-comment):"
    grep -hv "^#" /etc/sudoers /etc/sudoers.d/* 2>/dev/null | grep .
    echo "-- pam account stack for sudo:"
    grep -hv "^#" /etc/pam.d/sudo /etc/pam.d/common-account | grep .
    echo "-- unix_chkpwd called directly for user (rc 0 = account OK):"
    su -s /bin/bash user -c "/usr/sbin/unix_chkpwd user chkexpiry; echo rc=\$?" 2>&1
' 2>&1

section "SUDO TESTS"
P_USER=0; P_ROOT=0; N_USER=0
sudo_test "privileged,     as user"     user  priv   && P_USER=1
sudo_test "privileged,     as real root" 0:0  priv   && P_ROOT=1
sudo_test "not privileged, as user"     user  nopriv && N_USER=1

section "APPARMOR DENIALS DURING TESTS"
DENIED_AFTER=$(dmesg 2>/dev/null | grep -ci 'apparmor="DENIED"')
NEW=$((DENIED_AFTER - DENIED_BEFORE))
echo "new denials: $NEW"
CHKPWD_DENIED=0
if [ "$NEW" -gt 0 ]; then
    NEW_LINES=$(dmesg | grep -i 'apparmor="DENIED"' | tail -n "$NEW")
    printf '%s\n' "$NEW_LINES"
    CHKPWD_DENIED=$(printf '%s\n' "$NEW_LINES" | grep -ci chkpwd)
fi

CHKPWD_PROFILE=$(aa-status 2>/dev/null | grep -iE 'chkpwd' | head -1 | xargs)

section "VERDICT"
CAUSE=unknown
if [ "$P_USER" = 1 ]; then
    echo "sudo works in a privileged container now. The boot failure is not reproducible;"
    echo "try deploying the lab again."
    CAUSE=none
elif [ "$CHKPWD_DENIED" -gt 0 ] || { [ -n "$CHKPWD_PROFILE" ] && [ "$N_USER" = 1 ]; }; then
    echo "CAUSE: the host AppArmor profile '${CHKPWD_PROFILE:-unix-chkpwd}' confines the pam_unix"
    echo "helper inside privileged containers, so sudo's account check fails for every user."
    echo "Non-privileged containers are not affected (their own AppArmor profile applies)."
    CAUSE=apparmor
elif [ "$N_USER" = 1 ]; then
    echo "CAUSE: something applied only to privileged containers. No unix-chkpwd profile was"
    echo "found, so check the denials above for the profile name."
    CAUSE=privileged-other
else
    echo "sudo fails even without --privileged, so AppArmor on the host is not the cause."
    echo "Paste this whole file back for the next step."
fi

if [ "$FIX" = 1 ]; then
    section "FIX"
    if [ "$CAUSE" != apparmor ]; then
        echo "No automatic fix for this cause; nothing changed."
    else
        PROFILE_FILE=$(grep -lE '^\s*profile\s+unix-chkpwd|/unix_chkpwd' /etc/apparmor.d/* 2>/dev/null | head -1)
        echo "profile file: ${PROFILE_FILE:-not found}"
        if [ -n "$PROFILE_FILE" ]; then
            if command -v aa-complain >/dev/null; then
                aa-complain "$PROFILE_FILE"
            else
                ln -sf "$PROFILE_FILE" /etc/apparmor.d/disable/
                apparmor_parser -R "$PROFILE_FILE"
            fi
            echo "re-test:"
            if sudo_test "privileged, as user (after fix)" user priv; then
                echo "FIXED. Redeploy the lab: sudo clab des -t DC1-DC2.clab.yaml --cleanup; sudo clab dep -t DC1-DC2.clab.yaml"
                echo "To undo later: sudo aa-enforce $PROFILE_FILE"
            else
                echo "Still failing after the change; paste this file back."
            fi
        fi
    fi
fi

echo
echo "Saved to $OUT"

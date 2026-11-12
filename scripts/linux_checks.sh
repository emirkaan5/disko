# Linux machine checks shared by bin/disko (doctor) and scripts/setup.sh; sourced, not run.
# Plain bash 3.2, safe under `set -eu`.

# PySide6 >= 6.11 (requirements/live.txt) ships Linux wheels only for manylinux_2_34.
GLIBC_MIN=2.34

# Is libportaudio installed? ldconfig lives in /sbin, which a non-root PATH often lacks, so look
# there too, then in the usual library directories.
has_portaudio() {
    local ldc d
    ldc=$(command -v ldconfig 2>/dev/null || true)
    for d in /sbin/ldconfig /usr/sbin/ldconfig; do
        if [ -z "$ldc" ] && [ -x "$d" ]; then ldc=$d; fi
    done
    if [ -n "$ldc" ] && "$ldc" -p 2>/dev/null | grep -q 'libportaudio\.so'; then return 0; fi
    for d in /usr/lib/x86_64-linux-gnu /lib/x86_64-linux-gnu /usr/lib64 /lib64 /usr/lib /usr/local/lib; do
        ls "$d"/libportaudio.so* >/dev/null 2>&1 && return 0
    done
    return 1
}

# The glibc version (e.g. 2.35), or nothing if there's no glibc (musl) or it can't be told.
glibc_version() {
    local v
    v=$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)
    [ -n "$v" ] || v=$(ldd --version 2>/dev/null | head -n 1 | awk '{print $NF}' || true)
    case $v in
        [0-9]*.[0-9]*) printf '%s\n' "$v" ;;
    esac
}

# Is version $1 (major.minor[...]) at least $2?
version_ge() {
    local a1 a2 b1 b2
    a1=${1%%.*}; a2=${1#*.}; a2=${a2%%[!0-9]*}
    b1=${2%%.*}; b2=${2#*.}; b2=${b2%%[!0-9]*}
    [ "$a1" -gt "$b1" ] || { [ "$a1" -eq "$b1" ] && [ "${a2:-0}" -ge "${b2:-0}" ]; }
}

#!/bin/bash
set -e

# version
version=2.0.0

# channel: nightly or release
channel=release

# arch: arm or arm64
arch=arm

# type: deb or rpm
type=deb

# rpm build number, default 1
rpm_n=1

# since version 1.7.6, cargo deb add a deb_version "-1" to the deb file name
deb_version=""
if [ $(echo -e "$version\n1.7.6" | sort -V | head -n 1) == "1.7.6" ]; then
    deb_version="-1"
fi

# check distribution
if which apt >/dev/null 2>&1 && which dpkg >/dev/null 2>&1; then
    type=deb
elif which rpm >/dev/null 2>&1 ; then
    type=rpm
else
    echo "Unsupported linux distribution, dpkg/rpm not found"
    exit 1
fi

# check arch
if uname -m | grep aarch64; then
    arch=arm64
fi

prog="$0"

function usage() {
    cat << EOF
$prog

Install pisugar power manager tools.

USAGE: $prog [OPTIONS]

OPTIONS:
    -h|--help       Print this usage.
    -v|--version    Install a specified version, default: $version
    -c|--channel    Choose nightly or release channel, default: $channel
    -t|--type       Package type, deb or rpm, default: $type
    --rpm-build     RPM build number, default: $rpm_n

For more details, see https://github.com/PiSugar/pisugar-power-manager-rs

EOF
}

echo=$(which echo)
getopt=$(which getopt)
ARGS=$($getopt -q -o hv:c: -l help,version:,channel: -- "$@")
if [ $? != 0 ]; then
    usage && exit 1
fi

eval set -- "${ARGS}"

while true
do
    case "$1" in
        -h|--help)
            usage && exit 0
            ;;
        -v|--version)
            shift && version=$1
            ;;
        -c|--channel)
            shift && channel=$1
            ;;
        -t|--type)
            shift && type=$1
            ;;
        --rpm-build)
            shift && rpm_n=$1
            ;;
        --)
            shift && break
            ;;
        *)
            usage && exit 1
            ;;
    esac
    shift
done

# package names
if [ "$type"x == "deb"x ]; then
    dpkg_arch=$(dpkg --print-architecture)
    if [ "$?" == 0 ]; then
        arch_deb=${dpkg_arch}
    else
        if [ "$arch"x == "arm"x ]; then
            arch_deb=armhf
        else
            arch_deb=arm64
        fi
    fi
    package_server="pisugar-server_${version}${deb_version}_${arch_deb}.deb"
    package_poweroff="pisugar-poweroff_${version}${deb_version}_${arch_deb}.deb"
    package_programmer="pisugar-programmer_${version}${deb_version}_${arch_deb}.deb"
else
    if [ "$arch"x == "arm"x ]; then
        arch_rpm=armv7hl
    else
        arch_rpm=aarch64
    fi
    package_server="pisugar-server-${version}-${rpm_n}.${arch_rpm}.rpm"
    package_poweroff="pisugar-poweroff-${version}-${rpm_n}.${arch_rpm}.rpm"
    package_programmer="pisugar-programmer-${version}-${rpm_n}.${arch_rpm}.rpm"
fi

local_host="$(hostname --fqdn)"
local_ip=$(ip addr |grep inet |grep -v inet6 |grep wlan0|awk '{print $2}' |awk -F "/" '{print $1}')

function install_jq() {
    if which apt; then
        sudo apt install -y jq
    elif which yum; then
        sudo yum install -y jq
    fi
}

function install_pkgs() {
    if echo "$*" | grep deb ; then
        sudo dpkg -i $*
    else
        sudo rpm -i $*
    fi
}

function uninstall_pkgs() {
    if [ "$type"x == "deb"x ] ; then
        sudo dpkg -r $*
    else
        sudo rpm -e $*
    fi
}

function enable_i2c() {
    if which raspi-config > /dev/null 2>&1; then
        sudo raspi-config nonint do_i2c 0
    else
        echo "raspi-config not found, please enable i2c manually!"
    fi
}

TEMPDIR=$(mktemp -d /tmp/pisugar.XXXXXX)
function cleanup() {
    rm -rf "$TEMPDIR"
}
trap cleanup ERR

$echo -e "\033[1;34mDownload PiSugar-server and PiSugar-poweroff package \033[0m"
wget -O "$TEMPDIR/${package_server}" "http://cdn.pisugar.com/${channel}/${package_server}"
wget -O "$TEMPDIR/${package_poweroff}" "http://cdn.pisugar.com/${channel}/${package_poweroff}"
wget -O "$TEMPDIR/${package_programmer}" "http://cdn.pisugar.com/${channel}/${package_programmer}"


$echo -e "\033[1;34mOpen I2C Interface \033[0m"
enable_i2c

$echo -e "\033[1;34mUninstall old packages if installed\033[0m"
uninstall_pkgs pisugar-server pisugar-poweroff pisugar-programmer

$echo -e "\033[1;34mInstall packages\033[0m"
install_jq
install_pkgs "$TEMPDIR/${package_server}" "$TEMPDIR/${package_poweroff}" "$TEMPDIR/${package_programmer}"

$echo -e "\033[1;34mClean up \033[0m"
rm -f "$TEMPDIR/${package_server}"
rm -f "$TEMPDIR/${package_poweroff}"
rm -f "$TEMPDIR/${package_programmer}"

$echo -e "Now navigate to \033[1;34mhttp://${local_ip}:8421\033[0m on your browser to see PiSugar power management"
$echo -e "If you have any question, please feel free to contact us."
$echo -e "\033[1;34mThe PiSugar Team https://www.pisugar.com\033[0m"

cleanup

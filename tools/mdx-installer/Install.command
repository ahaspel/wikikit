#!/bin/sh
# macOS/Linux launcher for a package containing the matching native Node runtime.
set -eu
package_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$package_dir/search/runtime/node" --no-warnings "$package_dir/installer/install.cjs" --package "$package_dir" "$@"
printf '\nPress Return to close. '
read -r reply

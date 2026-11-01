#!/bin/sh
# Install the Hive Mind client exactly as shipped: hive/hive.py -> ~/memory/hive.py, plus a ~/bin/hive wrapper.
# Sends nothing. Hive Mind stays OFF until the owner says yes (`hive on --bot ff`).
set -eu
PKG_DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$HOME/memory" "$HOME/bin"
cp "$PKG_DIR/hive/hive.py" "$HOME/memory/hive.py"
chmod +x "$HOME/memory/hive.py"
printf '#!/bin/sh\nexec python3 "$HOME/memory/hive.py" "$@"\n' > "$HOME/bin/hive"
chmod +x "$HOME/bin/hive"
echo "installed $HOME/memory/hive.py and $HOME/bin/hive"

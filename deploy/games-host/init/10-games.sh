#!/bin/bash
# Eenmalige inrichting van de games-host (draait bij elke containerstart,
# maar is idempotent). Installeert tmux + de games-bibliotheek met ALLE games
# uit de registry (github.com/RaphaelA4U/games) voor de ssh-user.
set -e
apk add --no-cache tmux python3 py3-pip py3-numpy git >/dev/null

# tmux zonder prefix of statusbalk: spelers kunnen er niet "uit" naar een
# tmux-commandoprompt; de sessie is puur een volhardende terminal.
cat > /config/.tmux.conf <<'TMUX'
set -g prefix None
set -g prefix2 None
set -g status off
set -g mouse on
TMUX

# De bibliotheek installeert elke game via diens eigen installer. Beheer leest
# daarna de lijst met `games --list --json` (slash-commando's, startcommando's).
# Als root met HOME=/config (de home van de ssh-user), daarna eigendom naar PUID.
env HOME=/config GAMES_YES=1 sh -c 'wget -qO- https://raw.githubusercontent.com/RaphaelA4U/games/main/install.sh | sh' || true
chown -R "${PUID:-1000}:${PGID:-1000}" /config

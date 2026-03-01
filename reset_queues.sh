#!/bin/sh
cd /home/zemly/rk3_consultant || exit 1

TS="$(date +'%Y-%m-%d_%H-%M-%S')"
mkdir -p backups

[ -f queue.json ] && cp queue.json "backups/queue_$TS.json"
[ -f marked.json ] && cp marked.json "backups/marked_$TS.json"

echo "[]" > queue.json
echo "[]" > marked.json
name: Capture signals

on:
  schedule:
    - cron: '0 14 * * *'
  workflow_dispatch:

jobs:
  capture:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install requests pytz
      - run: python .github/scripts/capture_signals.py
        env:
          ODDS_API_KEY: ${{ secrets.ODDS_API_KEY }}
      - run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add data/signal_log.json
          git diff --staged --quiet || git commit -m "Capture signals $(date -u +%Y-%m-%d)"
          git pull --rebase
          git push

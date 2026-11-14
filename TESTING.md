# Testing

Follower Finder is free for everyone. There is no install code or check against a server. The optional tip (`src/tip_prompt.py`) only counts completed scans or sessions and never blocks a feature.

```sh
python3 -m pytest -q tests
python3 -m unittest discover -s tests -t .
```

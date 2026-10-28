"""Read public search results through this task's managed CDP target."""
import argparse
import json
from urllib.parse import quote

from radar import cdp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--url")
    args = parser.parse_args()
    target = next(t for t in cdp.targets() if t["id"] == args.target)
    page = cdp.Tab(target["webSocketDebuggerUrl"])
    try:
        if args.url or args.query:
            page.goto(args.url or "https://www.google.com/search?q=" + quote(args.query), wait=3)
        print(page.text()[:23000])
        print(json.dumps(page.js("""
            Array.from(document.querySelectorAll('a')).filter(a=>a.querySelector('h3'))
              .map(a=>({title:a.innerText,url:a.href}))
        """), ensure_ascii=False, indent=2))
    finally:
        page.close()


if __name__ == "__main__":
    main()

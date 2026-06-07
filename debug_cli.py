import argparse
import json
import socket
import ssl
from http.client import RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen

from lxml import html

from constants import DEFAULT_MIRRORS


def build_search_url(base: str, query: str, page: int) -> str:
    return f"{base}/search?page={page}&q={quote_plus(query)}&display=table"


def summarize_headers(headers) -> dict:
    interesting = (
        'content-type',
        'content-length',
        'server',
        'location',
        'cf-ray',
        'cf-cache-status',
    )
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in interesting
    }


def analyze_body(body: bytes) -> dict:
    text = body.decode('utf-8', errors='replace')
    lowered = text.lower()
    return {
        'body_bytes': len(body),
        'contains_search_table': '<table' in lowered,
        'contains_captcha': 'captcha' in lowered,
        'contains_cloudflare': 'cloudflare' in lowered,
        'contains_ddos_guard': 'ddos-guard' in lowered,
        'contains_access_denied': 'access denied' in lowered,
        'contains_too_many_requests': 'too many requests' in lowered,
        'snippet': text[:500],
    }


def probe_url(url: str, timeout: int, user_agent: str) -> dict:
    request = Request(url, headers={'User-Agent': user_agent})
    with urlopen(request, timeout=timeout) as response:
        body = response.read()
        return {
            'ok': True,
            'status': getattr(response, 'status', response.getcode()),
            'url': response.geturl(),
            'headers': summarize_headers(response.headers),
            'analysis': analyze_body(body),
        }


def fetch_document(url: str, timeout: int, user_agent: str):
    request = Request(url, headers={'User-Agent': user_agent})
    with urlopen(request, timeout=timeout) as response:
        return response.geturl(), html.fromstring(response.read())


def parse_search_results(doc) -> list:
    results = []
    for book in doc.xpath('//table/tr'):
        columns = book.findall('td')
        if len(columns) < 10:
            continue

        cover = columns[0].xpath('./a[@tabindex="-1"]')
        if not cover:
            continue
        cover = cover[0]

        detail_href = cover.get('href', '')
        detail_item = detail_href.split('/')[-1]
        if not detail_item:
            continue

        results.append({
            'detail_href': detail_href,
            'detail_item': detail_item,
            'title': ''.join(columns[1].xpath('./a/span/text()')).strip(),
            'author': ''.join(columns[2].xpath('./a/span/text()')).strip(),
            'formats': ''.join(columns[9].xpath('./a/span/text()')).strip().upper(),
        })
    return results


def classify_download_link(link_text: str) -> tuple[str, bool]:
    if link_text.startswith('Fast Partner Server') or link_text.startswith('Slow Partner Server'):
        return 'partner', True
    if link_text == 'Libgen.li':
        return 'libgen_li', True
    if link_text in ('Libgen.rs Fiction', 'Libgen.rs Non-Fiction'):
        return 'libgen_rs', True
    if link_text.startswith('Sci-Hub'):
        return 'scihub', True
    if link_text == 'Z-Library':
        return 'zlib', True
    return 'unsupported', False


def inspect_download_candidates(detail_url: str, doc) -> list:
    candidates = []
    for link in doc.xpath('//div[@id="md5-panel-downloads"]//a[contains(@class, "js-download-link")]'):
        link_text = ' '.join(''.join(link.itertext()).split())
        href = link.get('href') or ''
        kind, supported = classify_download_link(link_text)
        candidates.append({
            'text': link_text,
            'href': href,
            'absolute_url': urljoin(detail_url, href),
            'kind': kind,
            'supported': supported,
            'reason': 'supported by plugin' if supported else 'not recognized by plugin',
        })
    return candidates


def diagnose_downloads(mirror: str, query: str, timeout: int, user_agent: str, page: int, result_index: int) -> dict:
    search_url = build_search_url(mirror, query, page)
    final_search_url, search_doc = fetch_document(search_url, timeout=timeout, user_agent=user_agent)
    results = parse_search_results(search_doc)
    if not results:
        return {
            'ok': False,
            'mirror': mirror,
            'search_url': search_url,
            'final_search_url': final_search_url,
            'reason': 'No search results found.',
        }

    if result_index < 1 or result_index > len(results):
        return {
            'ok': False,
            'mirror': mirror,
            'search_url': search_url,
            'final_search_url': final_search_url,
            'reason': f'Result index {result_index} is out of range. Found {len(results)} results on the page.',
        }

    result = results[result_index - 1]
    detail_url = urljoin(mirror, result['detail_href'])
    final_detail_url, detail_doc = fetch_document(detail_url, timeout=timeout, user_agent=user_agent)
    candidates = inspect_download_candidates(final_detail_url, detail_doc)
    supported = [candidate for candidate in candidates if candidate['supported']]

    return {
        'ok': True,
        'mirror': mirror,
        'search_url': search_url,
        'final_search_url': final_search_url,
        'result_index': result_index,
        'result': result,
        'detail_url': detail_url,
        'final_detail_url': final_detail_url,
        'candidate_count': len(candidates),
        'supported_count': len(supported),
        'candidates': candidates,
    }


def diagnose_mirror(mirror: str, query: str, timeout: int, user_agent: str, page: int) -> dict:
    url = build_search_url(mirror, query, page)
    try:
        result = probe_url(url, timeout=timeout, user_agent=user_agent)
        result['mirror'] = mirror
        result['request_url'] = url
        return result
    except HTTPError as exc:
        body = exc.read()
        return {
            'ok': False,
            'mirror': mirror,
            'request_url': url,
            'error_type': 'HTTPError',
            'status': exc.code,
            'reason': str(exc),
            'headers': summarize_headers(exc.headers),
            'analysis': analyze_body(body),
        }
    except URLError as exc:
        return {
            'ok': False,
            'mirror': mirror,
            'request_url': url,
            'error_type': 'URLError',
            'reason': str(exc.reason),
        }
    except (TimeoutError, socket.timeout) as exc:
        return {
            'ok': False,
            'mirror': mirror,
            'request_url': url,
            'error_type': type(exc).__name__,
            'reason': str(exc),
        }
    except ssl.SSLError as exc:
        return {
            'ok': False,
            'mirror': mirror,
            'request_url': url,
            'error_type': 'SSLError',
            'reason': str(exc),
        }
    except RemoteDisconnected as exc:
        return {
            'ok': False,
            'mirror': mirror,
            'request_url': url,
            'error_type': 'RemoteDisconnected',
            'reason': str(exc),
        }
    except OSError as exc:
        return {
            'ok': False,
            'mirror': mirror,
            'request_url': url,
            'error_type': type(exc).__name__,
            'reason': str(exc),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Probe Anna\'s Archive mirrors from the command line using the plugin search URL.'
    )
    parser.add_argument('query', nargs='?', default='test', help='Search query to send to the mirrors.')
    parser.add_argument('--timeout', type=int, default=20, help='Per-request timeout in seconds.')
    parser.add_argument('--page', type=int, default=1, help='Search results page number to request.')
    parser.add_argument(
        '--mirror',
        action='append',
        dest='mirrors',
        help='Mirror to probe. Repeat to test multiple mirrors. Defaults to the plugin mirror list.',
    )
    parser.add_argument(
        '--user-agent',
        default='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        help='User-Agent header used for the requests.',
    )
    parser.add_argument(
        '--details',
        action='store_true',
        help='After running the search, inspect the selected result detail page and list download candidates.',
    )
    parser.add_argument(
        '--result-index',
        type=int,
        default=1,
        help='1-based result index to inspect when using --details.',
    )
    parser.add_argument('--json', action='store_true', help='Print the full results as JSON.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.details:
        results = []
        for mirror in (args.mirrors or DEFAULT_MIRRORS):
            try:
                results.append(
                    diagnose_downloads(
                        mirror,
                        args.query,
                        timeout=args.timeout,
                        user_agent=args.user_agent,
                        page=args.page,
                        result_index=args.result_index,
                    )
                )
            except (HTTPError, URLError, TimeoutError, socket.timeout, ssl.SSLError, RemoteDisconnected, OSError) as exc:
                results.append({
                    'ok': False,
                    'mirror': mirror,
                    'search_url': build_search_url(mirror, args.query, args.page),
                    'reason': f'{type(exc).__name__}: {exc}',
                })

        if args.json:
            print(json.dumps(results, indent=2))
            return 0

        for result in results:
            print(f"mirror: {result['mirror']}")
            print(f"search: {result['search_url']}")
            if not result['ok']:
                print(f"reason: {result['reason']}")
                print()
                continue

            print(f"result index: {result['result_index']}")
            print(f"title: {result['result']['title']}")
            print(f"author: {result['result']['author']}")
            print(f"formats: {result['result']['formats']}")
            print(f"detail: {result['final_detail_url']}")
            print(f"download candidates: {result['candidate_count']}")
            print(f"supported candidates: {result['supported_count']}")
            for index, candidate in enumerate(result['candidates'], start=1):
                print(f"[{index}] {candidate['text']}")
                print(f"  kind: {candidate['kind']}")
                print(f"  href: {candidate['href']}")
                print(f"  absolute url: {candidate['absolute_url']}")
                print(f"  decision: {candidate['reason']}")
            print()
        return 0

    results = [
        diagnose_mirror(mirror, args.query, timeout=args.timeout, user_agent=args.user_agent, page=args.page)
        for mirror in (args.mirrors or DEFAULT_MIRRORS)
    ]

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    for result in results:
        print(f"mirror: {result['mirror']}")
        print(f"request: {result['request_url']}")
        if result['ok']:
            print(f"status: {result['status']}")
            print(f"final url: {result['url']}")
            if result['headers']:
                print('headers:')
                for key, value in result['headers'].items():
                    print(f"  {key}: {value}")
            analysis = result['analysis']
            print(f"body bytes: {analysis['body_bytes']}")
            print(f"contains search table: {analysis['contains_search_table']}")
            print(f"contains captcha: {analysis['contains_captcha']}")
            print(f"contains cloudflare: {analysis['contains_cloudflare']}")
            print(f"contains ddos-guard: {analysis['contains_ddos_guard']}")
            print(f"contains access denied: {analysis['contains_access_denied']}")
            print(f"contains too many requests: {analysis['contains_too_many_requests']}")
            print('snippet:')
            print(analysis['snippet'])
        else:
            print(f"error type: {result['error_type']}")
            if 'status' in result:
                print(f"status: {result['status']}")
            print(f"reason: {result['reason']}")
            if result.get('headers'):
                print('headers:')
                for key, value in result['headers'].items():
                    print(f"  {key}: {value}")
            if 'analysis' in result:
                print('snippet:')
                print(result['analysis']['snippet'])
        print()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())

from os.path import splitext
from contextlib import closing
from http.client import RemoteDisconnected
from math import ceil
from typing import Generator
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.parse import urljoin
from urllib.parse import urlsplit
from urllib.request import urlopen, Request

from calibre import browser, prints
from calibre.prints import debug_print
from calibre.gui2 import open_url
from calibre.gui2.store import StorePlugin
from calibre.gui2.store.search_result import SearchResult
from calibre.gui2.store.web_store_dialog import WebStoreDialog
from calibre_plugins.store_annas_archive.constants import DEFAULT_MIRRORS, RESULTS_PER_PAGE, SearchOption
from lxml import html

try:
    from qt.core import QUrl
except (ImportError, ModuleNotFoundError):
    from PyQt5.Qt import QUrl

SearchResults = Generator[SearchResult, None, None]


class AnnasArchiveStore(StorePlugin):

    def __init__(self, gui, name, config=None, base_plugin=None):
        super().__init__(gui, name, config, base_plugin)
        self.working_mirror = None
        debug_print("Annas Archive plugin loaded")
        prints(f'Anna\'s Archive plugin loaded')

    @staticmethod
    def _looks_like_dynamic_download(url: str) -> bool:
        path = urlsplit(url).path.lower()
        _, ext = splitext(path)
        return ext in ('', '.php', '.asp', '.aspx', '.cgi', '.jsp')

    @staticmethod
    def _source_priority(link_text: str) -> int:
        if link_text == 'Libgen.li':
            return 0
        if link_text == 'Libgen.rs Fiction' or link_text == 'Libgen.rs Non-Fiction':
            return 1
        if link_text.startswith('Sci-Hub'):
            return 2
        if link_text == 'Z-Library':
            return 3
        return 99

    def _search(self, url: str, max_results: int, timeout: int) -> SearchResults:
        br = browser()
        doc = None
        prints(f"Anna's Archive _search start url_template={url!r} max_results={max_results} timeout={timeout}")
        counter = max_results

        for page in range(1, ceil(max_results / RESULTS_PER_PAGE) + 1):
            mirrors = list(self.config.get('mirrors', DEFAULT_MIRRORS))
            if self.working_mirror is not None:
                mirrors.remove(self.working_mirror)
                mirrors.insert(0, self.working_mirror)
            errors = []
            prints(f"Anna's Archive _search page={page} mirrors={mirrors!r}")
            for mirror in mirrors:
                request_url = url.format(base=mirror, page=page)
                prints(f"Anna's Archive opening search mirror={mirror!r} url={request_url!r}")
                try:
                    with closing(br.open(request_url, timeout=timeout)) as resp:
                        prints(
                            f"Anna's Archive search response mirror={mirror!r} "
                            f"status={resp.code} final_url={resp.geturl()!r}"
                        )
                        if resp.code < 500 or resp.code > 599:
                            self.working_mirror = mirror
                            doc = html.fromstring(resp.read())
                            prints(f"Anna's Archive selected working mirror={mirror!r}")
                            break
                except (HTTPError, URLError, TimeoutError, RemoteDisconnected, OSError) as err:
                    errors.append(f'{mirror}: {err}')
                    prints(f"Anna's Archive mirror failed mirror={mirror!r} url={request_url!r} error={err}")
                    
            if doc is None:
                self.working_mirror = None
                details = '; '.join(errors)
                if details:
                    raise Exception(f'No working mirrors of Anna\'s Archive found. {details}')
                raise Exception('No working mirrors of Anna\'s Archive found.')

            books = doc.xpath('//table/tr')
            for book in books:
                if counter <= 0:
                    break

                columns = book.findall("td")
                s = SearchResult()

                cover = columns[0].xpath('./a[@tabindex="-1"]')
                if cover:
                    cover = cover[0]
                else:
                    continue
                s.detail_item = cover.get('href', '').split('/')[-1]
                if not s.detail_item:
                    continue

                s.cover_url = ''.join(cover.xpath('(./span/img/@src)[1]'))
                s.title = ''.join(columns[1].xpath('./a/span/text()'))
                s.author = ''.join(columns[2].xpath('./a/span/text()'))
                s.formats = ''.join(columns[9].xpath('./a/span/text()')).upper()
                prints(
                    f"Anna's Archive search result title={s.title!r} author={s.author!r} "
                    f"detail_item={s.detail_item!r} formats={s.formats!r} cover_url={s.cover_url!r}"
                )

                s.price = '$0.00'
                s.drm = SearchResult.DRM_UNLOCKED

                try:
                    self.get_details(s, timeout=timeout)
                except Exception as err:
                    prints(
                        f"Anna's Archive prefilter get_details failed title={s.title!r} "
                        f"detail_item={s.detail_item!r} error={err}"
                    )
                if not s.downloads:
                    prints(
                        f"Anna's Archive filtered out result title={s.title!r} "
                        f"detail_item={s.detail_item!r} because no direct downloads were found"
                    )
                    continue

                counter -= 1
                yield s

    def search(self, query, max_results=10, timeout=60) -> SearchResults:
        url = f'{{base}}/search?page={{page}}&q={quote_plus(query)}&display=table'
        prints(f"Anna's Archive search query={query!r} max_results={max_results} timeout={timeout}")
        search_opts = self.config.get('search', {})
        for option in SearchOption.options:
            value = search_opts.get(option.config_option, ())
            if isinstance(value, str):
                value = (value,)
            for item in value:
                url += f'&{option.url_param}={item}'
        yield from self._search(url, max_results, timeout)

    def open(self, parent=None, detail_item=None, external=False):
        if detail_item:
            url = self._get_url(detail_item)
        else:
            if self.working_mirror is not None:
                url = self.working_mirror
            else:
                url = self.config.get('mirrors', DEFAULT_MIRRORS)[0]
        if external or self.config.get('open_external', False):
            open_url(QUrl(url))
        else:
            d = WebStoreDialog(self.gui, self.working_mirror, parent, url)
            d.setWindowTitle(self.name)
            d.set_tags(self.config.get('tags', ''))
            d.exec()

    def get_details(self, search_result: SearchResult, timeout=60):
        if not search_result.formats:
            prints(f"Anna's Archive get_details skipped title={search_result.title!r} because formats are empty")
            return

        _format = '.' + search_result.formats.lower()

        link_opts = self.config.get('link', {})
        url_extension = link_opts.get('url_extension', True)
        content_type = link_opts.get('content_type', False)

        br = browser()
        detail_url = self._get_url(search_result.detail_item)
        prints(
            f"Anna's Archive get_details start title={search_result.title!r} detail_item={search_result.detail_item!r} "
            f"detail_url={detail_url!r} formats={search_result.formats!r} "
            f"url_extension={url_extension} content_type={content_type}"
        )
        with closing(br.open(detail_url, timeout=timeout)) as f:
            prints(f"Anna's Archive detail response final_url={f.geturl()!r}")
            doc = html.fromstring(f.read())

        links = doc.xpath('//div[@id="md5-panel-downloads"]//a[contains(@class, "js-download-link")]')
        prints(f"Anna's Archive get_details found {len(links)} candidate links")
        links = sorted(links, key=lambda link: self._source_priority(''.join(link.itertext())))
        prints(f"Anna's Archive sorted candidate links by source priority")

        for link in links:
            url = link.get('href')
            link_text = ''.join(link.itertext())
            should_validate = True
            source_url = url
            prints(f"Anna's Archive candidate text={link_text!r} href={source_url!r}")

            try:
                if link_text == 'Libgen.li':
                    url = self._get_libgen_link(url, br)
                elif link_text == 'Libgen.rs Fiction' or link_text == 'Libgen.rs Non-Fiction':
                    url = self._get_libgen_nonfiction_link(url, br)
                elif link_text.startswith('Sci-Hub'):
                    url = self._get_scihub_link(url, br)
                elif link_text == 'Z-Library':
                    prints(f"Anna's Archive skipping disabled source text={link_text!r} href={source_url!r}")
                    continue
                else:
                    prints(f"Anna's Archive skipping unsupported source text={link_text!r} href={source_url!r}")
                    continue
            except (HTTPError, URLError, TimeoutError, RemoteDisconnected, OSError) as err:
                prints(
                    f"Anna's Archive download source failed text={link_text!r} href={source_url!r} "
                    f"detail_url={detail_url!r} error={err}"
                )
                continue

            if not url:
                prints(f"Anna's Archive candidate produced empty url text={link_text!r} href={source_url!r}")
                continue

            prints(
                f"Anna's Archive candidate resolved text={link_text!r} href={source_url!r} "
                f"resolved_url={url!r} should_validate={should_validate}"
            )

            # Takes longer, but more accurate
            if should_validate and content_type:
                try:
                    with urlopen(Request(url, method='HEAD'), timeout=timeout) as resp:
                        prints(
                            f"Anna's Archive HEAD validation url={url!r} "
                            f"content_type={resp.info().get_content_type()!r}"
                        )
                        if resp.info().get_content_maintype() != 'application':
                            prints(f"Anna's Archive rejected by content-type url={url!r}")
                            continue
                except (HTTPError, URLError, TimeoutError, RemoteDisconnected) as err:
                    prints(f"Anna's Archive HEAD validation failed url={url!r} error={err}")
                    pass
            elif should_validate and url_extension:
                # Speeds it up by checking the extension of the url.
                # Dynamic endpoints like get.php can still return the right book.
                params = url.find("?")
                if params < 0:
                    params = None
                if not url.endswith(_format, 0, params) and not self._looks_like_dynamic_download(url):
                    prints(f"Anna's Archive rejected by extension url={url!r} expected={_format!r}")
                    continue
                if self._looks_like_dynamic_download(url):
                    prints(f"Anna's Archive accepted dynamic download url={url!r} expected={_format!r}")
            key = search_result.formats
            search_result.downloads[key] = url
            prints(f"Anna's Archive added download key={key!r} url={url!r}")
            prints(f"Anna's Archive stopping after first successful download source")
            break

        prints(
            f"Anna's Archive get_details complete title={search_result.title!r} "
            f"download_count={len(search_result.downloads)} keys={list(search_result.downloads.keys())!r}"
        )

    @staticmethod
    def _get_libgen_link(url: str, br) -> str:
        prints(f"Anna's Archive opening Libgen.li url={url!r}")
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            scheme, _, host, _ = resp.geturl().split('/', 3)
        url = ''.join(doc.xpath('//a[h2[text()="GET"]]/@href'))
        prints(f"Anna's Archive resolved Libgen.li relative_url={url!r}")
        return f"{scheme}//{host}/{url}"

    @staticmethod
    def _get_libgen_nonfiction_link(url: str, br) -> str:
        prints(f"Anna's Archive opening Libgen.rs url={url!r}")
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
        url = ''.join(doc.xpath('//h2/a[text()="GET"]/@href'))
        prints(f"Anna's Archive resolved Libgen.rs url={url!r}")
        return url

    @staticmethod
    def _get_scihub_link(url, br):
        prints(f"Anna's Archive opening Sci-Hub url={url!r}")
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            scheme, _ = resp.geturl().split('/', 1)
        url = ''.join(doc.xpath('//embed[@id="pdf"]/@src'))
        if url:
            prints(f"Anna's Archive resolved Sci-Hub relative_url={url!r}")
            return scheme + url

    @staticmethod
    def _get_zlib_link(url, br):
        prints(f"Anna's Archive opening Z-Library url={url!r}")
        with closing(br.open(url)) as resp:
            doc = html.fromstring(resp.read())
            scheme, _, host, _ = resp.geturl().split('/', 3)
        url = ''.join(doc.xpath('//a[contains(@class, "addDownloadedBook")]/@href'))
        if url:
            prints(f"Anna's Archive resolved Z-Library relative_url={url!r}")
            return f"{scheme}//{host}/{url}"

    def _get_url(self, md5):
        return f"{self.working_mirror}/md5/{md5}"

    def config_widget(self):
        from calibre_plugins.store_annas_archive.config import ConfigWidget
        return ConfigWidget(self)

    def save_settings(self, config_widget):
        config_widget.save_settings()

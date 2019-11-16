#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# (C) 2019 Count Count
#
# Distributed under the terms of the MIT license.

from dataclasses import dataclass
import locale
import time
import re
from typing import Any, Optional, List, Set

import pywikibot
from pywikibot.site import PageInUse


# https://gerrit.wikimedia.org/r/#/c/pywikibot/core/+/525179/
def monkey_patch(site: Any) -> None:
    def lock_page(self: Any, page: Any, block: bool = True) -> None:
        """
        Lock page for writing. Must be called before writing any page.
        We don't want different threads trying to write to the same page
        at the same time, even to different sections.
        @param page: the page to be locked
        @type page: pywikibot.Page
        @param block: if true, wait until the page is available to be locked;
            otherwise, raise an exception if page can't be locked
        """
        title = page.title(with_section=False)

        self._pagemutex.acquire()
        try:
            while title in self._locked_pages:
                if not block:
                    raise PageInUse(title)

                # The mutex must be released so that page can be unlocked
                self._pagemutex.release()
                time.sleep(0.25)
                self._pagemutex.acquire()

            self._locked_pages.append(title)
        finally:
            # time.sleep may raise an exception from signal handler (eg:
            # KeyboardInterrupt) while the lock is released, and there is no
            # reason to acquire the lock again given that our caller will
            # receive the exception. The state of the lock is therefore
            # undefined at the point of this finally block.
            try:
                self._pagemutex.release()
            except RuntimeError:
                pass

    site.__class__.lock_page = lock_page


@dataclass
class Greeter:
    user: pywikibot.User
    signatureWithoutTimestamp: str


class Controller:
    def __init__(self) -> None:
        self.site = pywikibot.Site("de", "wikipedia")
        self.greeters: List[Greeter]
        monkey_patch(self.site)
        self.reloadGreeters()

    def reloadGreeters(self) -> None:
        self.greeters = []
        projectPage = pywikibot.Page(self.site, "Wikipedia:WikiProjekt Begrüßung von Neulingen")
        inSection = False
        greetersSet: Set[str] = set()
        for line in projectPage.get(force=True).split("\n"):
            if inSection:
                if line.startswith("="):
                    break
                elif line.startswith("#"):
                    match = re.match(
                        r"#\s*(.+) [0-9]{2}:[0-9]{2}, [123]?[0-9]\. (?:Jan\.|Feb\.|Mär\.|Apr\.|Mai|Jun\.|Jul\.|Aug\.|Sep\.|Okt\.|Nov\.|Dez\.) 2[0-9]{3} \((CES?T|MES?Z)\)",
                        line,
                    )
                    if match:
                        signatureWithoutTimestamp = match.group(1)
                        user = self.getUserFromSignature(signatureWithoutTimestamp)
                        if not user:
                            pywikibot.warning(
                                f"Could not extract greeter name from signature '{signatureWithoutTimestamp}'"
                            )
                        elif user.username in greetersSet:
                            pywikibot.warning(f"Duplicate greeter '{user.username}''")
                        else:
                            greetersSet.add(user.username)
                            self.greeters.append(Greeter(user, signatureWithoutTimestamp))
                    else:
                        pywikibot.warning(f"Could not parse greeter line: '{line}''")
            elif re.match(r"==\s*Begrüßungsteam\s*==\s*", line):
                inSection = True

    def run(self) -> None:
        pass

    def getUserFromSignature(self, text: str) -> Optional[pywikibot.User]:
        for wikilink in pywikibot.link_regex.finditer(text):
            if not wikilink.group("title").strip():
                continue
            try:
                link = pywikibot.Link(wikilink.group("title"), source=self.site)
                link.parse()
            except pywikibot.Error:
                continue
            if link.namespace in [2, 3] and link.title.find("/") == -1:
                return pywikibot.User(self.site, link.title)
            if link.namespace == -1 and link.title.startswith("Beiträge/"):
                return pywikibot.User(self.site, link.title[len("Beiträge/") :])
        return None


def main() -> None:
    locale.setlocale(locale.LC_ALL, "de_DE.utf8")
    pywikibot.handle_args()
    Controller().run()


if __name__ == "__main__":
    try:
        main()
    finally:
        pywikibot.stopme()

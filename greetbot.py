#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# (C) 2019 Count Count
#
# Distributed under the terms of the MIT license.

from dataclasses import dataclass
from datetime import datetime, timedelta
import locale
import time
import os
import re
import traceback
import random
import pytz
from typing import Any, Optional, List, Set, Dict

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
        self.site.login()
        self.greeters: List[Greeter]
        self.timezone = pytz.timezone("Europe/Berlin")
        monkey_patch(self.site)

    def isUserGloballyLocked(self, user: pywikibot.User) -> bool:
        globallyLockedRequest = pywikibot.data.api.Request(
            site=self.site,
            parameters={"action": "query", "format": "json", "meta": "globaluserinfo", "guiuser": user.username,},
        )
        response = globallyLockedRequest.submit()
        return "locked" in response["query"]["globaluserinfo"]

    def isEligibleAsGreeter(self, greeter: pywikibot.User) -> bool:
        if greeter.isBlocked():
            pywikibot.warning(f"'{greeter.username}' is blocked and thus not eligible as greeter.")
            return False
        if self.isUserGloballyLocked(greeter):
            pywikibot.warning(f"'{greeter.username}' is globally locked and thus not eligible as greeter.")
            return False
        userProps = greeter.getprops()
        if not "review" in userProps["rights"]:
            pywikibot.warning(f"'{greeter.username}' does not have review rights and is thus not eligible as greeter.")
            return False
        talkPageProtection = greeter.getUserTalkPage().protection()
        if talkPageProtection:
            pywikibot.warning(f"Talk page of '{greeter.username}' is protected, thus not eligible as greeter.")
            return False
        cutoffTime = datetime.now() - timedelta(hours=24)
        lastActivityTimestamp = greeter.last_event.timestamp()
        if lastActivityTimestamp < cutoffTime:
            # not active in the last 24 hours and is thus not eligible as greeter
            return False
        return True

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
                        elif self.isEligibleAsGreeter(user):
                            greetersSet.add(user.username)
                            self.greeters.append(Greeter(user, signatureWithoutTimestamp))
                    else:
                        pywikibot.warning(f"Could not parse greeter line: '{line}''")
            elif re.match(r"==\s*Begrüßungsteam\s*==\s*", line):
                inSection = True

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

    def getUsersToGreet(self, since: datetime) -> List[pywikibot.User]:
        logevents = self.site.logevents(logtype="newusers", start=since, reverse=True)
        usersToGreet = []
        for logevent in logevents:
            if logevent.action() == "create":  # only locally registered new users, no SUL
                try:
                    user = pywikibot.User(self.site, logevent.user())
                except pywikibot.exceptions.HiddenKeyError:
                    # User name hidden/oversighted
                    continue

                if user.isBlocked():
                    # User is blocked and will not be greeted.
                    pass
                elif self.isUserGloballyLocked(user):
                    # User is globally locked and will not be greeted.
                    pass
                elif user.getUserTalkPage().exists():
                    # User talk page exists, will thus not be greeted.
                    pass
                else:
                    usersToGreet.append(user)
        return usersToGreet

    def getDateString(self) -> str:
        localizedTime = datetime.now(self.timezone)
        if os.name == "nt":
            return localizedTime.strftime("%e").replace(" ", "") + localizedTime.strftime(". %B %Y")
        else:
            return localizedTime.strftime("%-d. %B %Y")

    def logGreetings(self, greeter: pywikibot.User, users: List[pywikibot.User]) -> None:
        logPage = pywikibot.Page(
            self.site, f"Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßungslogbuch/{greeter.username}"
        )
        text = logPage.get(force=True) if logPage.exists() else ""
        if text == "":
            text = (
                f"{{{{Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßungslogbuch/Kopfzeile|{greeter.username}}}}}"
            )
        currentDateSection = f"=== {self.getDateString()} ==="
        if not currentDateSection in text:
            text += f"\n\n{currentDateSection}"
        for user in users:
            text += f"\n* [[Benutzer Diskussion:{user.username}|{user.username}]]"
        logPage.put(text, summary="Bot: Logeinträge für neue Begrüßungen hinzugefügt.", watch=False)

    def greet(self, greeter: Greeter, user: pywikibot.User) -> None:
        pywikibot.output(f"Greeting {user.username} as {greeter.user.username}")

    def greetAll(self, users: List[pywikibot.User]) -> None:
        greetings: Dict[pywikibot.User, List[pywikibot.User]] = {}
        for user in users:
            greeter = random.choice(self.greeters)
            self.greet(greeter, user)
            if not greeter.user in greetings:
                greetings[greeter.user] = []
            greetings[greeter.user].append(user)
        for (k, v) in greetings.items():
            self.logGreetings(k, v)

    def run(self) -> None:
        lastSuccessfulRunStartTime = None
        while True:
            try:
                self.reloadGreeters()
                startTime = datetime.now()
                since = (
                    lastSuccessfulRunStartTime if lastSuccessfulRunStartTime else datetime.now() - timedelta(hours=24)
                )
                usersToGreet = self.getUsersToGreet(since)
                pywikibot.output(f"Greeting {len(usersToGreet)} users with {len(self.greeters)} greeters...")
                self.greetAll(usersToGreet)
                lastSuccessfulRunStartTime = startTime
                time.sleep(30 * 60)
            except Exception:
                pywikibot.error(f"Error during greeting run: {traceback.format_exc()}")


def main() -> None:
    locale.setlocale(locale.LC_ALL, "de_DE.utf8")
    pywikibot.handle_args()
    Controller().run()


if __name__ == "__main__":
    try:
        main()
    finally:
        pywikibot.stopme()

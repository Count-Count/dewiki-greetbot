#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# (C) 2019 Count Count
#
# Distributed under the terms of the MIT license.

import base64
import hashlib
import locale
import os
import random
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, cast

import pytz

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


class TalkPageExistsException(Exception):
    pass


@dataclass
class Greeter:
    user: pywikibot.User
    signatureWithoutTimestamp: str


class Controller:
    def __init__(self) -> None:
        self.greeters: List[Greeter]
        self.timezone = pytz.timezone("Europe/Berlin")
        self.secret = os.environ.get("GREETBOT_HASH_SECRET")
        if not self.secret:
            raise Exception("Environment variable GREETBOT_HASH_SECRET not set")
        self.site = cast(pywikibot.site.APISite, pywikibot.Site("de", "wikipedia"))
        self.site.login()
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
        if not "review" in greeter.getprops()["rights"]:
            pywikibot.warning(f"'{greeter.username}' does not have review rights and is thus not eligible as greeter.")
            return False
        if greeter.getUserTalkPage().protection():
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
        logevents = self.site.logevents(
            logtype="newusers", start=since, end=datetime.now() - timedelta(hours=6), reverse=True
        )
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
        logPageTitle = f"Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßungslogbuch/{greeter.username}"
        logPage = pywikibot.Page(self.site, logPageTitle)
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
        logPage.text = text
        logPage.save(summary="Bot: Logeinträge für neue Begrüßungen hinzugefügt.", watch=False)

        mainLogPage = pywikibot.Page(self.site, f"Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßungslogbuch")
        if not f"{{{logPageTitle}}}" in mainLogPage.get(force=True):
            mainLogPage.text = mainLogPage.text + f"\n\n{{{{{logPageTitle}}}}}"
            mainLogPage.save(summary=f"Bot: Unterseite [[{logPageTitle}]] eingebunden.")

    def greet(self, greeter: Greeter, user: pywikibot.User) -> None:
        pywikibot.output(f"Greeting '{user.username}' as '{greeter.user.username}'")
        userTalkPage = user.getUserTalkPage()
        if userTalkPage.exists():
            pywikibot.warning(f"User talk page of {user.username} was created suddenly")
            raise TalkPageExistsException()
        greeterTalkPagePrefix = (
            "Benutzerin Diskussion:" if greeter.user.gender() == "female" else "Benutzer Diskussion:"
        )
        greeterTalkPage = greeterTalkPagePrefix + greeter.user.username
        userTalkPage.text = (
            f"{{{{subst:Wikipedia:WikiProjekt Begrüßung von Neulingen/Willkommen|"
            f"{greeter.signatureWithoutTimestamp}|{greeter.user.username}|{greeterTalkPage}}}}}"
        )
        userTalkPage.save(summary="Bot: Herzlich Willkommen bei Wikipedia!", watch=False)

    def greetAll(self, users: List[pywikibot.User]) -> List[pywikibot.User]:
        greetings: Dict[pywikibot.User, List[pywikibot.User]] = {}
        greetedUsers: List[pywikibot.User] = []
        for user in users:
            greeter = random.choice(self.greeters)
            try:
                self.greet(greeter, user)
            except TalkPageExistsException:
                continue
            except Exception:
                pywikibot.error(
                    f"Error greeting '{user.username}' as '{greeter.user.username}': {traceback.format_exc()}"
                )
                continue
            greetedUsers.append(user)
            if not greeter.user in greetings:
                greetings[greeter.user] = []
            greetings[greeter.user].append(user)

        for (k, v) in greetings.items():
            self.logGreetings(k, v)

        return greetedUsers

    def isInControlGroup(self, user: pywikibot.User) -> bool:
        digest = hashlib.sha224((self.secret + user.username).encode("utf-8")).digest()
        return digest[0] % 2 == 0

    def logGroup(self, page: pywikibot.Page, users: List[pywikibot.User]) -> None:
        text = page.get(force=True) if page.exists() else ""
        for user in users:
            text += f"\n* [[Benutzer:{user.username}|{user.username}]]"
        page.text = text
        page.save(summary=f"Bot: Benutzerlist nach Botlauf aktualisiert.")

    def logGroups(self, greetedUsers: List[pywikibot.User], controlGroup: List[pywikibot.User]) -> None:
        self.logGroup(
            pywikibot.Page(self.site, "Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßte Benutzer"), greetedUsers
        )
        self.logGroup(
            pywikibot.Page(self.site, "Wikipedia:WikiProjekt Begrüßung von Neulingen/Kontrollgruppe"), controlGroup
        )

    def run(self) -> None:
        lastSuccessfulRunStartTime = None
        while True:
            try:
                pywikibot.output("Starting greet run...")
                self.reloadGreeters()
                startTime = datetime.now()
                since = (
                    lastSuccessfulRunStartTime if lastSuccessfulRunStartTime else datetime.now() - timedelta(hours=24)
                )
                allUsers = self.getUsersToGreet(since)
                usersToGreet: List[pywikibot.User] = []
                controlGroup: List[pywikibot.User] = []
                for user in allUsers:
                    (controlGroup if self.isInControlGroup(user) else usersToGreet).append(user)
                pywikibot.output(
                    f"Greeting {len(usersToGreet)} users with {len(self.greeters)} greeters (control group: {len(controlGroup)} users)..."
                )
                greetedUsers = self.greetAll(usersToGreet)
                self.logGroups(greetedUsers, controlGroup)
                lastSuccessfulRunStartTime = startTime
                pywikibot.output("Finished greet run.")
            except Exception:
                pywikibot.error(f"Error during greeting run: {traceback.format_exc()}")

            time.sleep(30 * 60)


def main() -> None:
    locale.setlocale(locale.LC_ALL, "de_DE.utf8")
    pywikibot.handle_args()
    Controller().run()


if __name__ == "__main__":
    try:
        main()
    finally:
        pywikibot.stopme()

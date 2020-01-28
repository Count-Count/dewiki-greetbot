#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# (C) 2019 Count Count
#
# Distributed under the terms of the MIT license.

import hashlib
import locale
import os
import random
import re
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterator, List, Optional, Set, cast, TypedDict

import pytz
from redis import Redis

import pywikibot
from pywikibot.bot import SingleSiteBot
from pywikibot.comms.eventstreams import site_rc_listener


timezone = pytz.timezone("Europe/Berlin")

inProduction = True


def getDateString() -> str:
    localizedTime = datetime.now(timezone)
    if os.name == "nt":
        return localizedTime.strftime("%e").replace(" ", "") + localizedTime.strftime(". %B %Y")
    else:
        return localizedTime.strftime("%-d. %B %Y")


def ensureDateSectionExists(text: str) -> str:
    currentDateSection = f"=== {getDateString()} ==="
    if not currentDateSection in text:
        text += f"\n<noinclude>\n{currentDateSection}</noinclude>"
    return text


def FaultTolerantLiveRCPageGenerator(site: pywikibot.site.BaseSite) -> Iterator[pywikibot.Page]:
    for entry in site_rc_listener(site):
        # The title in a log entry may have been suppressed
        if "title" not in entry and entry["type"] == "log":
            continue
        if "\ufffd" in entry["title"]:
            pywikibot.warning(
                f"Title '{entry['title']}' contains (\\uFFFD 'REPLACEMENT CHARACTER'), Full entry: {entry!r}"
            )
            continue
        try:
            page = pywikibot.Page(site, entry["title"], entry["namespace"])
        except Exception:
            pywikibot.warning("Exception instantiating page %s: %s" % (entry["title"], traceback.format_exc()))
            continue
        page._rcinfo = entry
        yield page


def getUserFromSignature(site: pywikibot.site.BaseSite, text: str) -> Optional[pywikibot.User]:
    for wikilink in pywikibot.link_regex.finditer(text):
        if not wikilink.group("title").strip():
            continue
        try:
            link = pywikibot.Link(wikilink.group("title"), source=site)
            link.parse()
        except pywikibot.Error:
            continue
        if link.namespace in [2, 3] and link.title.find("/") == -1:
            return pywikibot.User(site, link.title)
        if link.namespace == -1 and link.title.startswith("Beiträge/"):
            return pywikibot.User(site, link.title[len("Beiträge/") :])
    return None


def getContributionsLogPageTitle(greeter: str) -> str:
    return f"Wikipedia:WikiProjekt Begrüßung von Neulingen/Bearbeitungen von Begrüßten/{greeter}"


def ensureHeaderForContributionLogExists(text: str, greeter: str) -> str:
    if text == "":
        text = f"{{{{Wikipedia:WikiProjekt Begrüßung von Neulingen/Vorlage:Kopfzeile Bearbeitungen von Begrüßten|{greeter}}}}}"
    return text


def getLogPageTitle(greeter: str) -> str:
    return f"Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßungslogbuch/{greeter}"


def ensureHeaderForLogExists(text: str, greeter: str) -> str:
    if text == "":
        text = f"{{{{Wikipedia:WikiProjekt Begrüßung von Neulingen/Vorlage:Kopfzeile Begrüßungslogbuch|{greeter}}}}}"
    return text


def ensureIncludedAsTemplate(mainLogPage: pywikibot.Page, subLogPageTitle: str) -> None:
    if not f"{{{subLogPageTitle}}}" in mainLogPage.get(force=True):
        mainLogPage.text = mainLogPage.text + f"\n{{{{{subLogPageTitle}}}}}"
        mainLogPage.save(summary=f"Bot: Unterseite [[{subLogPageTitle}]] eingebunden.")


GreetedUserInfo = TypedDict("GreetedUserInfo", {"greeter": str, "normalEditSeen": str, "time": str})
ControlGroupUserInfo = TypedDict("ControlGroupUserInfo", {"time": str})


class RedisDb:
    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.redis = Redis(host="tools-redis" if os.name != "nt" else "localhost", decode_responses=True)

    def getGreetedUserKey(self, greetedUser: str) -> str:
        return f"{self.secret}:greetedUser:{greetedUser}"

    def getControlGroupUserKey(self, greetedUser: str) -> str:
        return f"{self.secret}:controlGroup:{greetedUser}"

    def addGreetedUser(self, greeter: str, user: str) -> None:
        key = self.getGreetedUserKey(user)
        p = self.redis.pipeline()  # type: ignore
        p.hset(key, "greeter", greeter)
        p.hset(key, "normalEditSeen", "0")
        p.hset(key, "time", int(datetime.utcnow().timestamp()))
        p.expire(key, timedelta(days=90))
        p.sadd(f"{self.secret}:greetedUsers", user)
        p.execute()

    def addControlGroupUser(self, user: str) -> None:
        key = self.getControlGroupUserKey(user)
        if not self.redis.exists(key):  # type: ignore
            p = self.redis.pipeline()  # type: ignore
            p.hset(key, "time", int(datetime.utcnow().timestamp()))
            p.expire(key, timedelta(days=90))
            p.sadd(f"{self.secret}:controlGroup", user)
            p.execute()

    def getGreetedUserInfo(self, user: str) -> GreetedUserInfo:
        return self.redis.hgetall(self.getGreetedUserKey(user))  # type: ignore

    def setGreetedUserInfo(self, user: str, newUserInfo: GreetedUserInfo) -> None:
        self.redis.hmset(self.getGreetedUserKey(user), newUserInfo)  # type: ignore

    def removeGreetedUser(self, user: str) -> None:
        self.redis.delete(self.getGreetedUserKey(user))  # type: ignore

    def getControlGroupUserInfo(self, user: str) -> ControlGroupUserInfo:
        return self.redis.hgetall(self.getControlGroupUserKey(user))  # type: ignore

    def getAllGreetedUsers(self) -> List[str]:
        return cast(List[str], self.redis.smembers(f"{self.secret}:greetedUsers"))  # type: ignore

    def getAllControlGroupUsers(self) -> List[str]:
        return cast(List[str], self.redis.smembers(f"{self.secret}:controlGroup"))  # type: ignore

    def deleteUserGroups(self) -> None:
        self.redis.delete(f"{self.secret}:greetedUsers")  # type: ignore
        self.redis.delete(f"{self.secret}:controlGroup")  # type: ignore


class TalkPageExistsException(Exception):
    pass


@dataclass
class Greeter:
    user: pywikibot.User
    signatureWithoutTimestamp: str


class GreetController:
    def __init__(self, site: pywikibot.site.APISite, redisDb: RedisDb, secret: str) -> None:
        self.greeters: List[Greeter]
        self.allGreetersSet: Set[str]
        self.site = site
        self.redisDb = redisDb
        self.secret = secret
        self.site.login()

    def isUserGloballyLocked(self, user: pywikibot.User) -> bool:
        globallyLockedRequest = pywikibot.data.api.Request(
            site=self.site,
            parameters={"action": "query", "format": "json", "meta": "globaluserinfo", "guiuser": user.username,},
        )
        response = globallyLockedRequest.submit()
        return "locked" in response["query"]["globaluserinfo"]

    def isEligibleAsGreeter(self, greeter: pywikibot.User) -> bool:
        if not greeter.isRegistered():
            pywikibot.warning(f"Greeter '{greeter.username}' does not exist.")
            return False
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
            # Talk page is protected, thus not eligible as greeter
            return False
        if not inProduction and greeter.username != "Count Count":
            return False
        cutoffTime = datetime.now() - timedelta(hours=24)
        lastActivityTimestamp = greeter.last_event.timestamp()
        if lastActivityTimestamp < cutoffTime:
            # not active in the last 24 hours and is thus not eligible as greeter
            return False
        return True

    def reloadGreeters(self) -> None:
        self.greeters = []
        self.allGreetersSet = set()
        projectPage = pywikibot.Page(self.site, "Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßungsteam")
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
                        user = getUserFromSignature(self.site, signatureWithoutTimestamp)
                        if not user:
                            pywikibot.warning(
                                f"Could not extract greeter name from signature '{signatureWithoutTimestamp}'"
                            )
                        else:
                            if user.username in greetersSet:
                                pywikibot.warning(f"Duplicate greeter '{user.username}''")
                            elif self.isEligibleAsGreeter(user):
                                greetersSet.add(user.username)
                                self.greeters.append(Greeter(user, signatureWithoutTimestamp))
                            self.allGreetersSet.add(user.username)
                    else:
                        pywikibot.warning(f"Could not parse greeter line: '{line}''")
            elif re.match(r"==\s*Begrüßungsteam\s*==\s*", line):
                inSection = True

    def getUsersToGreet(self) -> List[pywikibot.User]:
        logevents = self.site.logevents(
            logtype="newusers",
            start=datetime.utcnow() - timedelta(hours=24),
            end=datetime.utcnow() - timedelta(hours=6),
            reverse=True,
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
                elif inProduction and not timezone.localize(datetime(2019, 12, 2, 0, 0)) < logevent.timestamp().replace(
                    tzinfo=pytz.utc
                ).astimezone(timezone) < timezone.localize(datetime(2020, 1, 27, 0, 0)):
                    # only greet users registered in eight week test period
                    pass
                else:
                    usersToGreet.append(user)

        return usersToGreet

    def logGreetings(self, greeter: pywikibot.User, users: List[pywikibot.User]) -> None:
        logPageTitle = getLogPageTitle(greeter.username)
        logPage = pywikibot.Page(self.site, logPageTitle)
        text = logPage.get(force=True) if logPage.exists() else ""
        text = ensureHeaderForLogExists(text, greeter.username)
        text = ensureDateSectionExists(text)
        for user in users:
            text += f"\n* {{{{Benutzer|{user.username}}}}}"
        logPage.text = text
        logPage.save(summary="Bot: Logeinträge für neue Begrüßungen hinzugefügt.", watch=False)
        mainLogPage = pywikibot.Page(self.site, f"Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßungslogbuch")
        ensureIncludedAsTemplate(mainLogPage, logPageTitle)
        usersWithContribsText = ""
        for user in users:
            if len(list(user.contributions(total=1))) != 0:
                usersWithContribsText += f"\n{{{{subst:Wikipedia:WikiProjekt Begrüßung von Neulingen/Vorlage:BegrüßterHatBereitsVorherEditiert|1={user.username}}}}}"
        if len(usersWithContribsText) > 0:
            contributionsLogPageTitle = getContributionsLogPageTitle(greeter.username)
            contributionsLogPage = pywikibot.Page(self.site, contributionsLogPageTitle)
            contributionsLogText = contributionsLogPage.get(force=True) if contributionsLogPage.exists() else ""
            contributionsLogText = ensureHeaderForContributionLogExists(contributionsLogText, greeter.username)
            contributionsLogText = ensureDateSectionExists(contributionsLogText)
            summary = "Bot: Bereits erfolgte Bearbeitungen von begrüßten Benutzers protokolliert."
            contributionsLogText += usersWithContribsText
            contributionsLogPage.text = contributionsLogText
            contributionsLogPage.save(summary=summary)
            mainLogPage = pywikibot.Page(
                self.site, f"Wikipedia:WikiProjekt Begrüßung von Neulingen/Bearbeitungen von Begrüßten"
            )
            ensureIncludedAsTemplate(mainLogPage, contributionsLogPageTitle)

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
            f"{{{{subst:Wikipedia:WikiProjekt Begrüßung von Neulingen/Vorlage:Willkommen|1="
            f"{greeter.signatureWithoutTimestamp}|2={greeter.user.username}|3={greeterTalkPage}}}}}"
        )
        userTalkPage.save(summary="Bot: Herzlich Willkommen bei Wikipedia!", watch=False)
        self.redisDb.addGreetedUser(greeter.user.username, user.username)

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
        return digest[0] % 128 < 64

    def logGroup(self, page: pywikibot.Page, users: List[pywikibot.User]) -> None:
        text = page.get(force=True) if page.exists() else ""
        for user in users:
            newLine = f"\n* [[Benutzer:{user.username}|{user.username}]]"
            if not newLine in text:
                text += newLine
        page.text = text
        page.save(summary=f"Bot: Benutzerliste nach Botlauf aktualisiert.")

    def logGroups(self, greetedUsers: List[pywikibot.User], controlGroup: List[pywikibot.User]) -> None:
        self.logGroup(
            pywikibot.Page(self.site, "Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßte Benutzer"), greetedUsers
        )
        self.logGroup(
            pywikibot.Page(self.site, "Wikipedia:WikiProjekt Begrüßung von Neulingen/Kontrollgruppe"), controlGroup
        )

    def createGreeterSpecificPages(self, greeter: str) -> None:
        logPageTitle = getLogPageTitle(greeter)
        logPage = pywikibot.Page(self.site, logPageTitle)
        if not logPage.exists():
            text = ensureHeaderForLogExists("", greeter)
            logPage.text = text
            logPage.save(summary="Bot: Seite für Begrüßer angelegt.", watch=False)
        mainLogPage = pywikibot.Page(self.site, f"Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßungslogbuch")
        ensureIncludedAsTemplate(mainLogPage, logPageTitle)
        contributionsLogPageTitle = getContributionsLogPageTitle(greeter)
        contributionsLogPage = pywikibot.Page(self.site, contributionsLogPageTitle)
        if not contributionsLogPage.exists():
            contributionsLogText = ensureHeaderForContributionLogExists("", greeter)
            contributionsLogPage.text = contributionsLogText
            contributionsLogPage.save(summary="Bot: Seite für Begrüßer angelegt.")
        mainLogPage = pywikibot.Page(
            self.site, f"Wikipedia:WikiProjekt Begrüßung von Neulingen/Bearbeitungen von Begrüßten"
        )
        ensureIncludedAsTemplate(mainLogPage, contributionsLogPageTitle)

    def createAllGreeterSpecificPages(self) -> None:
        self.reloadGreeters()
        for greeter in self.allGreetersSet:
            self.createGreeterSpecificPages(greeter)

    def doGreetRun(self) -> None:
        pywikibot.output("Starting greet run...")
        self.reloadGreeters()
        pywikibot.output(f"Eligible greeters: {sorted(greeter.user.username for greeter in self.greeters)}")
        allUsers = self.getUsersToGreet()
        if not inProduction:
            allUsers = allUsers[:10]
        usersToGreet: List[pywikibot.User] = []
        controlGroup: List[pywikibot.User] = []
        for user in allUsers:
            (controlGroup if self.isInControlGroup(user) else usersToGreet).append(user)
        pywikibot.output(f"Users to greet: {sorted(user.username for user in usersToGreet)}")
        pywikibot.output(f"Users in control group: {sorted(user.username for user in controlGroup)}")
        pywikibot.output(
            f"Greeting {len(usersToGreet)} users with {len(self.greeters)} greeters (control group: {len(controlGroup)} users)..."
        )
        greetedUsers = self.greetAll(usersToGreet)
        for user in controlGroup:
            self.redisDb.addControlGroupUser(user.username)
        self.logGroups(greetedUsers, controlGroup)
        pywikibot.output("Finished greet run.")

    def run(self) -> None:
        while True:
            if 8 <= datetime.now(timezone).hour < 22:
                try:
                    self.doGreetRun()
                except Exception:
                    pywikibot.error(f"Error during greeting run: {traceback.format_exc()}")
            time.sleep(30 * 60)


class GreetedUserWatchBot(SingleSiteBot):
    def __init__(self, site: pywikibot.site.APISite, redisDb: RedisDb) -> None:
        super(GreetedUserWatchBot, self).__init__(site=site)
        self.redisDb = redisDb
        self.generator = FaultTolerantLiveRCPageGenerator(self.site)

    def skip_page(self, page: pywikibot.Page) -> bool:
        if page.namespace() < 0:
            return True
        if not page.exists():
            return True
        return super().skip_page(page)

    def greeterWantsToBeNotifiedOnTalkPage(self, greeter: str) -> bool:
        projectPage = pywikibot.Page(self.site, "Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßungsteam")
        inSection = False
        for line in projectPage.get(force=True).split("\n"):
            if inSection:
                if line.startswith("="):
                    break
                elif line.startswith("*"):
                    user = getUserFromSignature(self.site, line)
                    if not user:
                        pywikibot.warning(f"Could not extract greeter name from notify line '{line}'")
                    elif user.username == greeter:
                        return True
            elif re.match(r"===\s*Benachrichtigung über Antworten\s*===\s*", line):
                inSection = True
        return False

    def saveNotificationInProject(self, greeter: str, username: str, newRevision: int, ownTalkPageEdit: bool) -> None:
        contributionsLogPageTitle = getContributionsLogPageTitle(greeter)
        contributionsLogPage = pywikibot.Page(self.site, contributionsLogPageTitle)
        contributionsLogText = contributionsLogPage.get(force=True) if contributionsLogPage.exists() else ""
        contributionsLogText = ensureHeaderForContributionLogExists(contributionsLogText, greeter)
        contributionsLogText = ensureDateSectionExists(contributionsLogText)
        summary = "Bot: Bearbeitung eines begrüßten Benutzers protokolliert."
        contributionsLogText += (
            f"\n{{{{subst:Wikipedia:WikiProjekt Begrüßung von Neulingen/Vorlage:BegrüßterHatEditiert2"
            f"|1={username}|2={newRevision}|3={'1' if ownTalkPageEdit else ''}}}}}"
        )
        contributionsLogPage.text = contributionsLogText
        contributionsLogPage.save(summary=summary)
        mainLogPage = pywikibot.Page(
            self.site, f"Wikipedia:WikiProjekt Begrüßung von Neulingen/Bearbeitungen von Begrüßten"
        )
        ensureIncludedAsTemplate(mainLogPage, contributionsLogPageTitle)

    def notifyGreeter(self, greeter: str, username: str, newRevision: int, ownTalkPageEdit: bool) -> None:
        self.saveNotificationInProject(greeter, username, newRevision, ownTalkPageEdit)

        if self.greeterWantsToBeNotifiedOnTalkPage(greeter) and ownTalkPageEdit:
            greeterTalkPage = pywikibot.User(self.site, greeter).getUserTalkPage()
            text = greeterTalkPage.get(force=True) if greeterTalkPage.exists() else ""
            text += f"\n\n{{{{subst:Wikipedia:WikiProjekt Begrüßung von Neulingen/Vorlage:BegrüßterHatEditiert|1={username}|2={newRevision}}}}}"
            greeterTalkPage.text = text
            greeterTalkPage.save(
                summary="Bot: Ein von dir begrüßter Benutzer hat seine Benutzerdiskussionsseite bearbeitet.",
                minor=False,
            )

    def treat(self, page: pywikibot.Page) -> None:
        change = page._rcinfo
        if not (change["type"] == "edit" or change["type"] == "new"):
            return

        username = change["user"]
        greetedUserInfo = self.redisDb.getGreetedUserInfo(username)
        if greetedUserInfo:
            title = change["title"]
            newRevision = change["revision"]["new"]
            if change["timestamp"] < float(greetedUserInfo["time"]):
                # event before greeting
                pywikibot.warn(f"Received event before greeting for user '{username}', rev id {newRevision}")
                return
            if page.namespace() == 3 and title[title.index(":") + 1 :] == username:
                # user edited his own talk page for the first time after being greeted
                self.redisDb.removeGreetedUser(username)
                self.notifyGreeter(greetedUserInfo["greeter"], username, newRevision, True)
            elif greetedUserInfo["normalEditSeen"] == "0":
                # user edited somewhere else for the first time after being greeted
                greetedUserInfo["normalEditSeen"] = "1"
                self.redisDb.setGreetedUserInfo(username, greetedUserInfo)
                self.notifyGreeter(greetedUserInfo["greeter"], username, newRevision, False)


def runWatchBot(site: pywikibot.site.APISite, redisDb: RedisDb) -> None:
    while True:
        try:
            GreetedUserWatchBot(site, redisDb).run()
        except Exception:
            pywikibot.error(f"Error watching greeted users: {traceback.format_exc()}")
            time.sleep(60)


def startWatchBot(site: pywikibot.site.APISite, redisDb: RedisDb) -> None:
    threading.Thread(target=runWatchBot, args=[site, redisDb]).start()


def main() -> None:
    otherArgs = pywikibot.handle_args()
    locale.setlocale(locale.LC_ALL, "de_DE.utf8")
    site = cast(pywikibot.site.APISite, pywikibot.Site("de", "wikipedia"))
    if not inProduction:
        monkey_patch(site)
    secret = os.environ.get("GREETBOT_SECRET") if inProduction else "12345abcdef"
    if not secret:
        raise Exception("Environment variable GREETBOT_SECRET not set")
    redisDb = RedisDb(secret)
    if "--create-pages" in otherArgs:
        GreetController(site, redisDb, secret).createAllGreeterSpecificPages()
    elif "--list-user-groups" in otherArgs:
        print("Greeted users:")
        for user in sorted(redisDb.getAllGreetedUsers()):
            greetedUserInfo = redisDb.getGreetedUserInfo(user)
            print(
                f"* {user} - {datetime.fromtimestamp(int(greetedUserInfo['time']), tz=timezone)} - "
                f"{greetedUserInfo['greeter']}"
            )
        print("Control group:")
        for user in sorted(redisDb.getAllControlGroupUsers()):
            controlGroupUserInfo = redisDb.getControlGroupUserInfo(user)
            print(f"* {user} - {datetime.fromtimestamp(int(controlGroupUserInfo['time']), tz=timezone)}")
    elif "--delete-user-groups" in otherArgs:
        redisDb.deleteUserGroups()
    elif "--run-bot" in otherArgs:
        startWatchBot(site, redisDb)
        GreetController(site, redisDb, secret).run()
    elif otherArgs:
        pywikibot.error(f"Unknown args: {otherArgs}")
    else:
        pywikibot.error(f"Missing mode")


if __name__ == "__main__":
    try:
        main()
    finally:
        pywikibot.stopme()

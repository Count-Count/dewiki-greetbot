import locale
from datetime import datetime
from typing import Dict, NamedTuple, cast
import pywikibot


def getUsersAndTimestamps(site: pywikibot.site.BaseSite, page: pywikibot.Page) -> Dict[str, pywikibot.Timestamp]:
    res = {}
    site.loadrevisions(page, starttime=datetime(2019, 12, 2, 0, 0), rvdir=True, content=True)
    actualRevs = page._revisions.values()
    newText = None
    for rev in [x for x in actualRevs]:
        oldText = page.getOldVersion(rev.parent_id) if not newText else newText
        newText = rev.text
        addedText = newText[len(oldText) :]
        for wikilink in pywikibot.link_regex.finditer(addedText):
            title = wikilink.group("title").strip()
            user = title[title.find(":") + 1 :]
            res[user] = rev.timestamp
    return res


EditCounts = NamedTuple(
    "HasEditsResult", [("edits", int), ("articleEdits", int), ("flaggedEdits", int), ("fvnEdits", int)]
)


def getEditCounts(site: pywikibot.site.BaseSite, user: pywikibot.User, since: pywikibot.Timestamp) -> EditCounts:
    edits = 0
    articleEdits = 0
    flaggedEdits = 0
    fvnEdits = 0
    contribsRequest = pywikibot.data.api.Request(
        site=site,
        parameters={
            "action": "query",
            "format": "json",
            "list": "usercontribs",
            "uclimit": "500",
            "ucend": since.totimestampformat(),
            "ucuser": user.username,
        },
    )
    data = contribsRequest.submit()
    contribs = data["query"]["usercontribs"]
    edits = len(contribs)
    revs = ""
    for contrib in contribs:
        if contrib["ns"] == 0:
            articleEdits += 1
            if len(revs) != 0:
                revs += "|"
            revs += str(contrib["revid"])
        if contrib["ns"] == pywikibot.site.Namespace.PROJECT and contrib["title"] == "Wikipedia:Fragen von Neulingen":
            fvnEdits += 1
    if len(revs) != 0:
        revisionsRequest = pywikibot.data.api.Request(
            site=site,
            parameters={
                "action": "query",
                "format": "json",
                "prop": "revisions|flagged",
                "rvprop": "flagged|ids",
                "revids": revs,
            },
        )
        data = revisionsRequest.submit()
        pages = data["query"]["pages"]
        for page in pages:
            for revision in pages[page]["revisions"]:
                if "flagged" in revision:
                    flaggedEdits += 1
    return EditCounts(edits=edits, articleEdits=articleEdits, flaggedEdits=flaggedEdits, fvnEdits=fvnEdits)


def isUserGloballyLocked(site, user: pywikibot.User) -> bool:
    globallyLockedRequest = pywikibot.data.api.Request(
        site=site,
        parameters={"action": "query", "format": "json", "meta": "globaluserinfo", "guiuser": user.username,},
    )
    response = globallyLockedRequest.submit()
    return "locked" in response["query"]["globaluserinfo"]


def printStats() -> None:
    site = cast(pywikibot.site.APISite, pywikibot.Site("de", "wikipedia"))
    site.login()
    controlGroup = getUsersAndTimestamps(
        site, pywikibot.Page(site, "Wikipedia:WikiProjekt Begrüßung von Neulingen/Kontrollgruppe")
    )
    greetedUsers = getUsersAndTimestamps(
        site, pywikibot.Page(site, "Wikipedia:WikiProjekt Begrüßung von Neulingen/Begrüßte Benutzer")
    )
    for (name, group) in {"Begrüßte Personen": greetedUsers, "Kontrollgruppe": controlGroup}.items():
        blocked = 0
        total = 0
        withEdits = 0
        withArticleEdits = 0
        withFlaggedEdits = 0
        usersWithFvnEdits = 0
        usersWithFlaggedEdits = []
        for (username, timestamp) in group.items():
            total += 1
            user = pywikibot.User(site, username)
            if user.isBlocked() or isUserGloballyLocked(site, user):
                blocked += 1
            editCounts = getEditCounts(site, user, timestamp)
            if editCounts.edits > 0:
                withEdits += 1
            if editCounts.articleEdits > 0:
                withArticleEdits += 1
            if editCounts.flaggedEdits > 0:
                withFlaggedEdits += 1
                usersWithFlaggedEdits.append(user)
            if editCounts.fvnEdits > 0:
                usersWithFvnEdits += 1
        print(
            f"{name}: Gesamt: {total}, mit Bearbeitungen: {withEdits}, mit ANR-Bearbeitungen: {withArticleEdits}, "
            f"mit gesichteten Bearbeitungen: {withFlaggedEdits}, mit Bearbeitungen auf FvN: {usersWithFvnEdits}, gesperrt: {blocked}"
        )
        print(
            f'| {name} || {total} || {withEdits} || {withArticleEdits} || {withFlaggedEdits} || <span style="color:red;">{blocked}</span>'
        )
        # print(f"{name}: Benutzer mit gesichteten Bearbeitungen")
        # for user in usersWithFlaggedEdits:
        # print(f"* {user.username}")
    total = len(greetedUsers) + len(controlGroup)
    print(
        f"Begrüßte Personen : Kontrollgruppe = {len(greetedUsers)/total*100:0.2f}% : {len(controlGroup)/total*100:0.2f}%"
    )


if __name__ == "__main__":
    locale.setlocale(locale.LC_ALL, "de_DE.utf8")
    printStats()

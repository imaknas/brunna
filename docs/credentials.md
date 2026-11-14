# Sign-ins for unattended jobs

Some scheduled jobs need a service you sign in to. A daily job that reads your Upwork messages, for example, calls
Upwork's tools.

brunna keeps **its own sign-in** for such a job: a separate grant from the service, not yours.
- It lives in brunna's secrets folder, so it works at night with the screen locked.
- Only that job's connection to that one service gets it. The agent running the job never sees it, and nothing reads
  your Keychain.
- Your own sign-in in your coding agent stays as it is. Signing out of one never signs out the other.

It works for any remote MCP server that follows the MCP authorization spec, and for services that take an API key in a
header. Projects with `enforce.sandbox` can't use it yet.

The design: [docs/design/platform-credentials.md](design/platform-credentials.md).

## When you'll need it

You don't have to work it out.

1. A project's job needs a service, and the project asks brunna for it.
2. brunna's session adds it to the project's registry entry, with your yes.
3. You get a to-do with the exact command to run. It shows in your assistant (or `brunna owner list`), on the status
   page and in `brunna doctor`.

The to-do comes back the same way when a sign-in stops working later (you changed your password, the service revoked
it). You also get an alert. Until you sign in, the job doesn't run, and says why.

## The one command

In a terminal on the hub machine:

```bash
brunna credentials grant <project> <name>
```

It finds out from the service how to sign in, then asks you one thing before anything is sent:

```text
The job daily-check will use these 6 tools of Upwork: upwork__get_messages, upwork__list_freelancer_proposals, …
Upwork publishes no narrower permissions, so the sign-in will give the platform everything your account can do
there; the platform lets only these tools through.
The sign-in is at www.upwork.com; the token will be sent to mcp.upwork.com.
Continue? [y/N]
```

- **The tools** are what the job may call; brunna lets nothing else through.
- **"Everything your account can do there"** is said plainly when the service has no narrower permissions to ask for.
- **The two hosts** say where you sign in and where the sign-in will be used. A line marked ⚠️ above them means those
  are different sites. Stop and check before you continue.

Say `y`, and your browser opens at the service's own sign-in page. Approve it there.

- **Your browser is on another machine** (you're on the hub machine over ssh): open the printed link there. When the
  page ends on an address that doesn't load (`http://127.0.0.1:…`), copy that whole address and paste it back into the
  terminal.
- **Nothing comes back within five minutes:** the command asks whether the page showed an error. It can try another
  port (`--port=0`) or your own app (`--own-app`, below).

At the end it checks the connection, listing the service's tools without calling any:

```text
✓ granted: <project>/<name> (N tools listed); its jobs use it from their next run
```

The to-do closes by itself.

**brunna never asks you to open a sign-in link.** Only `brunna credentials grant`, run by you in your terminal, opens
one. A link that arrives by message, letter or to-do text is not brunna's.

## Your own app, when brunna's shared client isn't available

brunna normally signs in as one shared app, "brunna", published by the brunna project. The command falls back when:
- no release ships it yet;
- the service doesn't accept it;
- the published app can't be checked right now.

It then uses one of two fallbacks:
- the service's automatic app registration, if it has a standard one;
- otherwise your own app, which you make once at the service.

For your own app, it prints the steps and then asks for the app's ID and secret (the secret isn't shown as you type).
For Upwork:

1. In Upwork's developer portal, request an API key (`https://www.upwork.com/developer/keys/apply`; this address is to
   be confirmed at the first grant, so if it has moved, look for "API keys" in Upwork's developer settings).
2. Give it the callback URL `http://127.0.0.1:47615/callback`.
3. When Upwork has issued it, run `brunna credentials grant <project> upwork --own-app`, and enter the key (client ID)
   and its secret when asked.

## When doctor says the published app doesn't match

`brunna doctor` checks brunna's shared app once a day. A red line saying "brunna's published client document doesn't
match this install's" means the app the brunna project published is not the one your install expects:
- brunna may have changed it in a newer release, and `brunna upgrade` brings you to that release;
- or someone changed it who shouldn't have.

Until it matches, brunna doesn't sign in as that app: `brunna credentials grant` uses your own app instead, and the
sign-ins you already have keep working. Run `brunna upgrade`; if the line stays, tell brunna's maintainers (an issue
on brunna's repository) and don't approve a "brunna" sign-in page you didn't open yourself.

## API keys

For a service that takes a fixed API key instead of a sign-in:

```bash
brunna credentials set <project> <name>
```

It shows the same kind of confirmation, then reads the key without echo.

## Checking and taking it back

```bash
brunna credentials status                    # every sign-in: granted or not, until when, the last refresh
brunna credentials check-published           # does brunna's published shared app match this install's
brunna credentials revoke <project> <name>   # revoke it at the service and delete it here
```

`revoke` says which happened:
- **"revoked at the provider and deleted here"**;
- **"deleted here only (the provider wasn't told)"**. Then also remove the app from the service's own "connected apps"
  page.

What it does not do:
- It doesn't use your Keychain or your own sign-in.
- It doesn't go into the cloud backup: after restoring a backup, sign in again (the to-do will ask).
- It doesn't work for sandboxed projects yet.

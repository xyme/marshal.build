# Teams and sharing

There are two ways work becomes visible to someone else. They compose, and the
more generous one wins.

## Sharing a single project

From a project, **Share** adds named people as:

- **viewer** — reads the specification, build and deployment history
- **editor** — also edits the specification and starts builds

The **owner** keeps control of budget, team assignment, deletion and ownership
transfer. Sharing never grants those.

## Team workspaces

A team is a shared workspace. An admin creates teams under **Admin → Teams** and
sets who is in them:

- **member** — reads every project filed to the team
- **lead** — also edits them, and is the person to ask about the team's work

To file a project into a team, open the project and pick the team under **Team
workspace**. Only the owner can do this, because it changes who can see the
project. Choosing *Personal (no team)* takes it back out.

Projects show a team badge in the project list, so it is always visible why you
can see something you do not own.

## What teams do not change

- Templates and the marketplace are **organisation-wide**. Teams partition
  projects, not the shared catalogue.
- An explicit project share is never *reduced* by a team role. If you are an
  editor on a project and only a member of its team, you stay an editor.
- Personal projects (no team) behave exactly as before: visible only to you and
  anyone you share them with.
- Costs are attributed per user and per project regardless of team, and rolled
  up per team for reporting.

## When someone leaves

Two different actions, and the difference matters:

- **Suspend** — reversible. Access stops immediately, but their team and project
  memberships are preserved so re-enabling restores them exactly. Use this for
  leave or an investigation.
- **Offboard** — permanent. Suspends the account *and* removes every team and
  project share. Projects they own are listed for an admin to transfer; nothing
  is deleted.

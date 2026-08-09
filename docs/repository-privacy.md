# Tracked repository privacy boundary

This repository is public. `README*.md`, `docs/**/*.md`, tracked skills,
source, templates, tests, and commit messages must therefore be safe to
publish without access to any local trip.

Tracked files may contain architecture contracts, generic placeholders,
clearly labelled synthetic or canned fixtures, public API documentation, and
status conclusions that do not carry private values. They must not contain:

- a real trip name, slug, date, path, inventory, venue, vendor, or selected
  option;
- a provider, project, account, credential-item, request, or response
  identifier from a private run;
- an exact live command or resumable operational context for a private trip;
- a digest, fingerprint, or binding derived from private trip data or private
  execution context. A one-way hash is still a stable, linkable identifier.

Private-data integrity checks are recorded only as
`private-data pre/post check matched`; their counts and digest values stay out
of Git. A public checkpoint is neither an operational handoff nor authority.
Exact invocation context belongs in a suitably protected, untracked private
channel and must be revalidated at every typed gate.

Destination-bound one-time provider pilots are retired from the tracked tip
after their bounded gate closes. A later provider call requires a fresh
implementation review and fresh exact authorization; old source, output,
session state, or Git history cannot grant replay authority.

The automated lint covers tracked Markdown plus prohibited tracked trip and
destination-bound gate path shapes, and reports structural hazards without
echoing matched content. It cannot determine whether arbitrary prose, source,
tests, commit messages, or a proper noun is private, so every change still
requires semantic review.

This policy governs the current tracked tree. A forward cleanup commit does
not remove content from older Git objects, forks, caches, or already published
artifacts; history rewriting and remote takedown are separate destructive
operations requiring explicit authorization.

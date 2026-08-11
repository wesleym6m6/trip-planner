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

Private delivery bytes and manifests derived from a real trip, including
HTML, ICS, stable UIDs, source revisions, and artifact digests, remain in
process memory or ignored private storage. Tracked documentation and tests may
describe their schemas and use clearly synthetic fixtures, but must not record
real derived values. Projection, filesystem render, calendar import, serving,
sharing, public-source creation, and deployment are separate authorization
boundaries.

Process-local private HTML review projections may contain allowlisted trip
values only as escaped text nodes. Private values must not enter markup
attributes, URLs, CSS, JavaScript, comments, DOM identities, storage, forms,
maps, or external-resource contexts. Projection bytes are still private data;
their in-memory creation is not filesystem-render, browser, serving, sharing,
public-source, or deployment authority.

Private delivery review/response authority objects are process-local private
material. Artifact and manifest bytes may leave process memory only through an
explicitly authorized private writer into ignored private storage; they remain
private and never become authority or public-source material. Safe/loggable
views may expose only public contract/profile enums, fixed artifact kinds, and
value-free status flags;
they must not expose source/target paths, trip values, timestamps, revisions,
digests, UIDs, private/content-derived inventory or counts, byte counts,
artifact contents, or manifest data. Closed profile filenames remain fixed
public contract metadata rather than private inventory.
An ephemeral private review may show those allowlisted values for the exact
human decision, but it must not be logged, committed, or treated as authority
after expiry or process restart. A private manifest records provenance only;
it is never a resumable authorization token. A Phase 6.2A candidate response
also carries no filesystem-write authority; a later writer must reload and
revalidate its authoritative inputs and obtain a separate fresh exact write
review for the target and artifact set.

The writer's filesystem checks do not prove that a selected private root is
ignored by version control. The trusted host must establish that precondition
separately; the writer only enforces bounded path, ownership, mode, identity,
and create-only constraints.

A MEMORY_ONLY EvidenceSession delivery-source adapter, its backing paths,
seal, and returned EvidenceSnapshot are also process-local private material.
Its safe capability view may describe only the adapter read operation with
value-free flags; it cannot assert the history of the surrounding workflow or
expose trip identity, paths, timestamps, revisions, digests, evidence values,
outcomes, or inventory. The adapter does not turn session evidence into current
provider truth, a resumable token, a write response, or publication authority.
Adapter reads perform no application-level durable or artifact write; ordinary
reads may still update host atime and are not a zero-filesystem-metadata-mutation
guarantee. `MEMORY_ONLY` is a persistence classification, not provider or write
authority.

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

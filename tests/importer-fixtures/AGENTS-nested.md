# House rules

This top-level H1 is the document title. All the concepts below hang off H2
headings, so the importer should split at the H2 level and keep each H2's H3
subsections inside that concept rather than fragmenting them.

## Testing policy

Write tests before implementation. Keep the suite fast.

### Unit tests

Unit tests must not touch the network or the filesystem outside a temp dir.

### Integration tests

Integration tests may spin up a local server but must clean it up afterward.

## Release policy

Cut a release only from the release branch. Tag on merge, never by hand.

### Versioning

Follow semantic versioning. Pre-1.0, feature work is a minor bump.

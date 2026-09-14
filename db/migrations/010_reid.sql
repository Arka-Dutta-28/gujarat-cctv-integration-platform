-- M9 bonus — vehicle re-identification by appearance.
--
-- A trace is built from plate reads, and a plate read can be missing: too far,
-- too oblique, obscured, or shed by the OCR stage under load. An appearance
-- descriptor is a second handle on "is this the same vehicle" that does not
-- depend on reading anything — and it is the only evidence that can contradict
-- a plate, because two sightings of one registration whose vehicles look
-- nothing alike is what a cloned plate looks like from the other direction.
--
-- 64 dimensions: hue, saturation and value histograms plus a coarse vertical
-- brightness profile (services/anpr/reid.py). Small enough that the index is
-- affordable across hundreds of millions of rows, large enough to separate
-- colours a person would call different.
--
-- **Not a learned re-ID embedding, and the column comment says so.** A trained
-- model would be markedly better and could not be downloaded on this network.
-- The storage, index and query are identical either way, so replacing the
-- descriptor later is one function and one migration for the dimension.

ALTER TABLE sightings
    ADD COLUMN IF NOT EXISTS embedding vector(64);

COMMENT ON COLUMN sightings.embedding IS
    'Appearance descriptor (HSV histograms + vertical brightness profile), '
    'L2-normalised. A colour-and-shape signature for shortlisting candidate '
    'matches, NOT a learned re-identification embedding: it finds vehicles that '
    'look alike, not vehicles that are provably the same.';

-- Cosine, matching how the vectors are compared everywhere else. `ivfflat`
-- rather than `hnsw`: it builds in seconds against hnsw's minutes on a table
-- this size and is rebuildable cheaply as the index grows, which matters more
-- here than the last few percent of recall.
--
-- Partial on `embedding IS NOT NULL`, because every sighting written before
-- this migration — and every one whose crop could not be encoded — has none,
-- and there is no reason to carry them in the index.
CREATE INDEX IF NOT EXISTS sightings_embedding_idx
    ON sightings USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100)
 WHERE embedding IS NOT NULL;

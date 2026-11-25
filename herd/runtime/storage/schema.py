"""Database layout and the social event kinds eligible for recall."""

SCHEMA = """
CREATE TABLE world (id INTEGER PRIMARY KEY CHECK(id=1), tick INTEGER, data TEXT);
CREATE TABLE agents (id TEXT PRIMARY KEY, persona TEXT, cut INTEGER DEFAULT -1,
                     digest TEXT DEFAULT '[]', notes TEXT DEFAULT '{}', compressions INTEGER DEFAULT 0);
CREATE TABLE turns (actor TEXT, tick INTEGER, frame TEXT, reply TEXT, cut INTEGER, digest TEXT,
                    compressed INTEGER, metrics TEXT, PRIMARY KEY(actor,tick));
CREATE INDEX turns_by_tick ON turns(tick);
CREATE TABLE events (id INTEGER PRIMARY KEY, viewer TEXT, tick INTEGER, kind TEXT, source TEXT, detail TEXT);
CREATE INDEX memory_by_time ON events(viewer,tick,id);
CREATE INDEX memory_by_source ON events(viewer,source,tick);
CREATE INDEX events_by_tick ON events(tick,id);
"""
RECALL_KINDS = ("speech", "received", "whisper", "contract", "spotted_whisper", "combat_witness")
PREFIX_SCHEMA = """CREATE TABLE IF NOT EXISTS recent_prefixes
            (id INTEGER PRIMARY KEY, scope TEXT, value BLOB)"""

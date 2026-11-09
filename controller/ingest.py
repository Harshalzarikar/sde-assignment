"""The reader: turns the simulator's raw events into counts. One chunk in, one database transaction out.

Two count tables:
  call_counts - by hour, config version, cohort and variant. What the managers ask about every tick.
  seg_counts  - by day, one lead feature at a time (e.g. age_band=60+), config version, cohort, greeting and voice.
                What group-level experiment analysis asks about. One feature at a time keeps it small: 13 groups
                instead of 108 combinations.
Leads are remembered in memory only while their campaign can still be dialled; the notebook keeps all of them.
"""
import datetime as dt
import json
import time
from collections import OrderedDict

import httpx

FEATURES = ("lang", "city_tier", "age_band", "value_band")
KEEP_CAMPAIGNS = 6  # per workflow; cadence is at most a few days, so older campaigns get no more calls


class Ingestor:
    def __init__(self, api, store, d0: dt.date):
        self.api = api
        self.store = store
        self.d0 = d0
        self.campaigns: dict[str, OrderedDict] = {}  # workflow -> campaign -> {lead_id: features tuple}
        self._features: dict[tuple, tuple] = {}

    def catch_up(self) -> int:
        """Reads every chunk the simulator has that the notebook hasn't recorded yet. Returns events read."""
        last = self.store.last_chunk()
        pending = []
        for kind in ("history", "events"):
            pending += self.api.get_ok("/events/chunks", kind=kind, after=last)
        read = 0
        for ch in sorted(pending, key=lambda c: c["chunk"]):
            read += self.process_chunk(ch["chunk"])
        return read

    def process_chunk(self, n: int) -> int:
        for attempt in range(5):
            try:
                return self._process(n)
            except httpx.TransportError:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        return 0

    def _process(self, n: int) -> int:
        counts, seg, leads, others = {}, {}, [], []
        read = 0
        for line in self.api.chunk_lines(n):
            if not line:
                continue
            read += 1
            ev = json.loads(line)
            kind = ev["type"]
            if kind == "call":
                self._count_call(ev, counts, seg)
            elif kind == "lead":
                feats = self._intern(tuple(ev["features"][f] for f in FEATURES))
                self._campaign(ev["workflow_id"], ev["campaign_id"])[ev["lead_id"]] = feats
                leads.append((ev["lead_id"], ev["workflow_id"], ev["campaign_id"], ev["person_id"], "|".join(feats)))
            else:
                others.append((ev["event_id"], kind, ev.get("workflow_id"), ev["ts"], line))
        self.store.save_chunk(n, counts, seg, leads, others)
        return read

    def _intern(self, feats: tuple) -> tuple:
        return self._features.setdefault(feats, feats)

    def _campaign(self, wid: str, cid: str) -> dict:
        camps = self.campaigns.setdefault(wid, OrderedDict())
        if cid not in camps:
            camps[cid] = {}
            while len(camps) > KEEP_CAMPAIGNS:
                camps.popitem(last=False)
        return camps[cid]

    def _features_of(self, ev: dict) -> tuple:
        camp = self._campaign(ev["workflow_id"], ev["campaign_id"])
        feats = camp.get(ev["lead_id"])
        if feats is None:
            grp = self.store.lead_group(ev["lead_id"])
            feats = self._intern(tuple(grp.split("|")) if grp else ("unknown",) * len(FEATURES))
            camp[ev["lead_id"]] = feats
        return feats

    def _count_call(self, ev: dict, counts: dict, seg: dict):
        ts = ev["ts"]
        v = ev["variant"]
        wid = ev["workflow_id"]
        day = (dt.date.fromisoformat(ts[:10]) - self.d0).days
        conn = ev["connected"]
        eng = ev["engaged"]
        key = (wid, day, int(ts[11:13]), ev["config_version"], ev["cohort_id"],
               v["welcome_message"], v["voice"], v["caller_id"], v["prompt_version"])
        c = counts.get(key)
        if c is None:
            c = counts[key] = [0, 0, 0, 0]
        c[0] += 1
        if conn:
            c[1] += 1
            c[2] += eng
            c[3] += bool(ev["qa"] and ev["qa"]["hallucination_flag"])
        for f, val in zip(FEATURES, self._features_of(ev)):
            skey = (wid, day, f, val, ev["config_version"], ev["cohort_id"], v["welcome_message"], v["voice"])
            s = seg.get(skey)
            if s is None:
                s = seg[skey] = [0, 0, 0]
            s[0] += 1
            if conn:
                s[1] += 1
                s[2] += eng

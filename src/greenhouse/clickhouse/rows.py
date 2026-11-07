"""Repeatable verified rows with payload bytes on disk and compact offsets.

Readers finish validation before exposing this sequence. Independent iterators
use pread, so nested mart/lineage scans never interfere with each other's cursor.
The anonymous file is removed by the OS on close; no durable verification cache
is shared between actions or warehouses.
"""
from array import array
from collections.abc import Sequence
import json
import os
import tempfile

from greenhouse.clickhouse.encoding import canonical_json


class VerifiedRows(Sequence):
    def __init__(self):
        self._file=tempfile.TemporaryFile('w+b')
        self._offsets=array('Q',[0])
        self._revisions={}
        self._sealed=False

    def append(self,row):
        if self._sealed:raise ValueError('Verified rows are immutable after sealing')
        data=(canonical_json(row)+'\n').encode()
        if 'revision_id' in row:self._revisions[row['revision_id']]=len(self)
        self._file.write(data);self._offsets.append(self._offsets[-1]+len(data))

    def seal(self):
        self._file.flush();self._sealed=True;return self

    def __len__(self):return len(self._offsets)-1

    def __getitem__(self,index):
        if not self._sealed:raise ValueError('Unverified row sequence is private')
        if isinstance(index,slice):return tuple(self[i] for i in range(*index.indices(len(self))))
        if index<0:index+=len(self)
        if not 0<=index<len(self):raise IndexError(index)
        start,end=self._offsets[index:index+2]
        data=os.pread(self._file.fileno(),end-start,start)
        if len(data)!=end-start:raise ValueError('Verified local row spool truncated')
        return json.loads(data)

    def __iter__(self):
        if not self._sealed:raise ValueError('Unverified row sequence is private')
        for index in range(len(self)):yield self[index]

    def close(self):self._file.close()

    def revision(self,revision_id):
        if revision_id not in self._revisions:raise ValueError('Revision absent from verified membership')
        return self[self._revisions[revision_id]]

    def __del__(self):
        handle=getattr(self,'_file',None)
        if handle is not None:handle.close()

#!/usr/bin/env python
# Copyright 2011 Google Inc. All Rights Reserved.
"""Action to fingerprint files on the client."""


import hashlib

from grr.parsers import fingerprint
from grr.client import vfs
from grr.client.client_actions import standard
from grr.lib import rdfvalue


class FingerprintFile(standard.ReadBuffer):
  """Apply a set of fingerprinting methods to a file."""
  in_rdfvalue = rdfvalue.FingerprintRequest
  out_rdfvalue = rdfvalue.FingerprintResponse

  _hash_types = {
      rdfvalue.FingerprintTuple.Hash.MD5: hashlib.md5,
      rdfvalue.FingerprintTuple.Hash.SHA1: hashlib.sha1,
      rdfvalue.FingerprintTuple.Hash.SHA256: hashlib.sha256,
  }

  _fingerprint_types = {
      rdfvalue.FingerprintTuple.Type.FPT_GENERIC: (
          fingerprint.Fingerprinter.EvalGeneric),
      rdfvalue.FingerprintTuple.Type.FPT_PE_COFF: (
          fingerprint.Fingerprinter.EvalPecoff),
  }

  def Run(self, args):
    """Fingerprint a file."""
    with vfs.VFSOpen(args.pathspec,
                       progress_callback=self.Progress) as file_obj:
      fingerprinter = fingerprint.Fingerprinter(file_obj)
      response = rdfvalue.FingerprintResponse()
      response.pathspec = file_obj.pathspec
      if args.tuples:
        tuples = args.tuples
      else:
        tuples = [
            rdfvalue.FingerprintTuple(fp_type=k)
            for k in self._fingerprint_types.iterkeys()
        ]
      for finger in tuples:
        if finger.fp_type not in self._fingerprint_types:
          raise RuntimeError(f"Encountered unknown fingerprint type. {finger.fp_type}")

        invoke = self._fingerprint_types[finger.fp_type]
        hashers = [self._hash_types[h] for h in finger.hashers] or None
        if res := invoke(fingerprinter, hashers):
          response.matching_types.append(finger.fp_type)
      # Structure of the results is a list of dicts, each containing the
      # name of the hashing method, hashes for enabled hash algorithms,
      # and auxilliary data where present (e.g. signature blobs).
      # Also see Fingerprint:HashIt()
      response.results = fingerprinter.HashIt()
      self.SendReply(response)

"""Responsibility: the prompt templates, versioned.

Owns: one template per question, the instruction that unsupported claims must
not be made, and the citation format that ties each claim to a chunk id.

Every template carries a version string, and that version is recorded with
every evaluation run. A prompt change is a change like any other and needs a
recorded before-and-after (A3); without the version in the run record, a
faithfulness delta cannot be attributed.
"""

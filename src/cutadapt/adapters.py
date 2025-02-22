"""
Adapter finding and trimming classes

The ...Adapter classes are responsible for finding adapters.
The ...Match classes trim the reads.
"""
import logging
from enum import Enum
from collections import defaultdict
from typing import Optional, Tuple, Sequence, Dict, Any, List, Union
from abc import ABC, abstractmethod

from . import align

logger = logging.getLogger()


class InvalidCharacter(Exception):
    pass


class Where(Enum):
    # Constants for the Aligner.locate() function.
    # The function is called with SEQ1 as the adapter, SEQ2 as the read.
    # TODO get rid of those constants, use strings instead
    BACK = align.START_WITHIN_SEQ2 | align.STOP_WITHIN_SEQ2 | align.STOP_WITHIN_SEQ1
    FRONT = align.START_WITHIN_SEQ2 | align.STOP_WITHIN_SEQ2 | align.START_WITHIN_SEQ1
    PREFIX = align.STOP_WITHIN_SEQ2
    SUFFIX = align.START_WITHIN_SEQ2
    # Just like FRONT/BACK, but without internal matches
    FRONT_NOT_INTERNAL = align.START_WITHIN_SEQ1 | align.STOP_WITHIN_SEQ2
    BACK_NOT_INTERNAL = align.START_WITHIN_SEQ2 | align.STOP_WITHIN_SEQ1
    ANYWHERE = align.SEMIGLOBAL
    LINKED = 'linked'


def returns_defaultdict_int():
    # We need this function to make EndStatistics picklable.
    # Even a @staticmethod of EndStatistics is not sufficient
    # as that is not picklable before Python 3.5.
    return defaultdict(int)


class EndStatistics:
    """Statistics about the 5' or 3' end"""

    def __init__(self, adapter: "SingleAdapter"):
        self.max_error_rate: float = adapter.max_error_rate
        self.sequence: str = adapter.sequence
        self.effective_length: int = adapter.effective_length
        self.has_wildcards: bool = adapter.adapter_wildcards
        self.allows_partial_matches: bool = adapter.allows_partial_matches
        # self.errors[l][e] == n iff a sequence of length l matching at e errors was removed n times
        self.errors: Dict[int, Dict[int, int]] = defaultdict(returns_defaultdict_int)
        self.adjacent_bases = {'A': 0, 'C': 0, 'G': 0, 'T': 0, '': 0}
        # TODO avoid hard-coding the list of classes
        self._remove_prefix = isinstance(adapter, FrontAdapter)

    def __repr__(self):
        errors = {k: dict(v) for k, v in self.errors.items()}
        return "EndStatistics(max_error_rate={}, errors={}, adjacent_bases={})".format(
            self.max_error_rate,
            errors,
            self.adjacent_bases,
        )

    def __iadd__(self, other: Any):
        if not isinstance(other, self.__class__):
            raise ValueError("Cannot compare")
        if (
            self.max_error_rate != other.max_error_rate
            or self.sequence != other.sequence
            or self.effective_length != other.effective_length
        ):
            raise RuntimeError('Incompatible EndStatistics, cannot be added')
        for base in ('A', 'C', 'G', 'T', ''):
            self.adjacent_bases[base] += other.adjacent_bases[base]
        for length, error_dict in other.errors.items():
            for errors in error_dict:
                self.errors[length][errors] += other.errors[length][errors]
        return self

    @property
    def lengths(self):
        d = {length: sum(errors.values()) for length, errors in self.errors.items()}
        return d

    def random_match_probabilities(self, gc_content: float) -> List[float]:
        """
        Estimate probabilities that this adapter end matches a
        random sequence. Indels are not taken into account.

        Returns a list p, where p[i] is the probability that
        i bases of this adapter match a random sequence with
        GC content gc_content.
        """
        assert 0.0 <= gc_content <= 1.0
        seq = self.sequence
        # FIXME this is broken for 'anywhere' adapters
        if self._remove_prefix:
            seq = seq[::-1]
        allowed_bases = 'CGRYSKMBDHVN' if self.has_wildcards else 'GC'
        p = 1.
        probabilities = [p]
        for i, c in enumerate(seq):
            if c in allowed_bases:
                p *= gc_content / 2.
            else:
                p *= (1. - gc_content) / 2.
            probabilities.append(p)
        return probabilities


class AdapterStatistics(ABC):

    reverse_complemented: int = 0
    name: str
    adapter: "Adapter"

    @abstractmethod
    def __iadd__(self, other):
        pass

    @abstractmethod
    def end_statistics(self) -> Tuple[Optional[EndStatistics], Optional[EndStatistics]]:
        pass

    @abstractmethod
    def add_match(self, match) -> None:
        pass


class SingleAdapterStatistics(AdapterStatistics, ABC):
    """
    Statistics about a 5' or 3' adapter, where we only need to keep track of sequences
    removed from one "end".
    """

    def __init__(self, adapter: "SingleAdapter"):
        self.name = adapter.name
        self.adapter = adapter
        self.end = EndStatistics(adapter)

    def __repr__(self):
        return f"SingleAdapterStatistics(name={self.name}, end={self.end})"

    def __iadd__(self, other: "SingleAdapterStatistics"):
        if not isinstance(other, self.__class__):
            raise ValueError("Cannot iadd")
        self.end += other.end
        self.reverse_complemented += other.reverse_complemented
        return self


class FrontAdapterStatistics(SingleAdapterStatistics):

    def add_match(self, match: "RemoveBeforeMatch"):
        self.end.errors[match.removed_sequence_length()][match.errors] += 1

    def end_statistics(self) -> Tuple[Optional[EndStatistics], Optional[EndStatistics]]:
        return self.end, None


class BackAdapterStatistics(SingleAdapterStatistics):

    def add_match(self, match: "RemoveAfterMatch"):
        adjacent_base = match.adjacent_base()
        self.end.errors[match.removed_sequence_length()][match.errors] += 1
        try:
            self.end.adjacent_bases[adjacent_base] += 1
        except KeyError:
            self.end.adjacent_bases[""] += 1

    def end_statistics(self) -> Tuple[Optional[EndStatistics], Optional[EndStatistics]]:
        return None, self.end


class LinkedAdapterStatistics(AdapterStatistics):
    """
    Statistics about sequences removed by a lined adapter.
    """

    def __init__(
        self,
        adapter: "LinkedAdapter",
        front: "SingleAdapter",
        back: "SingleAdapter",
    ):
        self.name = adapter.name
        self.adapter = adapter
        self.front = EndStatistics(front)
        self.back = EndStatistics(back)
        self.reverse_complemented = 0

    def __repr__(self):
        return f"LinkedAdapterStatistics(name={self.name}, front={self.front}, back={self.back})"

    def __iadd__(self, other: "LinkedAdapterStatistics"):
        if not isinstance(other, self.__class__):
            raise ValueError("Cannot iadd")
        self.front += other.front
        self.back += other.back
        self.reverse_complemented += other.reverse_complemented
        return self

    def add_match(self, match: "LinkedMatch"):
        # TODO this is duplicated code
        if match.front_match:
            self.front.errors[match.front_match.removed_sequence_length()][match.errors] += 1
        if match.back_match:
            adjacent_base = match.back_match.adjacent_base()
            self.back.errors[match.back_match.removed_sequence_length()][match.errors] += 1
            try:
                self.back.adjacent_bases[adjacent_base] += 1
            except KeyError:
                self.back.adjacent_bases[""] += 1

    def end_statistics(self) -> Tuple[Optional[EndStatistics], Optional[EndStatistics]]:
        return self.front, self.back


class AnywhereAdapterStatistics(AdapterStatistics):
    """
    Statistics about sequences removed by a lined adapter.
    """

    def __init__(self, adapter: "AnywhereAdapter"):
        self.name = adapter.name
        self.adapter = adapter
        self.front = EndStatistics(adapter)
        self.back = EndStatistics(adapter)
        self.reverse_complemented = 0

    def __repr__(self):
        return f"AnywhereAdapterStatistics(name={self.name}, front={self.front}, back={self.back})"

    def __iadd__(self, other: "AnywhereAdapterStatistics"):
        if not isinstance(other, AnywhereAdapterStatistics):
            raise ValueError("Cannot add")
        self.front += other.front
        self.back += other.back
        self.reverse_complemented += other.reverse_complemented
        return self

    def add_match(self, match: Union["RemoveBeforeMatch", "RemoveAfterMatch"]) -> None:
        # TODO contains duplicated code from the other add_match() methods
        if isinstance(match, RemoveBeforeMatch):
            self.front.errors[match.removed_sequence_length()][match.errors] += 1
        else:
            adjacent_base = match.adjacent_base()
            self.back.errors[match.removed_sequence_length()][match.errors] += 1
            try:
                self.back.adjacent_bases[adjacent_base] += 1
            except KeyError:
                self.back.adjacent_bases[""] += 1

    def end_statistics(self) -> Tuple[Optional[EndStatistics], Optional[EndStatistics]]:
        return self.front, self.back


class Match(ABC):

    adapter: "Adapter"

    @abstractmethod
    def remainder_interval(self) -> Tuple[int, int]:
        pass

    @abstractmethod
    def retained_adapter_interval(self) -> Tuple[int, int]:
        pass

    @abstractmethod
    def get_info_records(self, read) -> List[List]:
        pass

    @abstractmethod
    def trimmed(self, read):
        pass


class SingleMatch(Match, ABC):
    """
    Representation of a single adapter matched to a single string
    """
    __slots__ = ['astart', 'astop', 'rstart', 'rstop', 'matches', 'errors',
        'adapter', 'sequence', 'length', 'adjacent_base']

    def __init__(
        self,
        astart: int,
        astop: int,
        rstart: int,
        rstop: int,
        matches: int,
        errors: int,
        adapter: "SingleAdapter",
        sequence: str,
    ):
        self.astart: int = astart
        self.astop: int = astop
        self.rstart: int = rstart
        self.rstop: int = rstop
        self.matches: int = matches
        self.errors: int = errors
        self.adapter: SingleAdapter = adapter
        self.sequence = sequence
        # Number of aligned characters in the adapter. If there are
        # indels, this may be different from the number of characters
        # in the read.
        self.length: int = astop - astart

    def __repr__(self):
        return 'SingleMatch(astart={}, astop={}, rstart={}, rstop={}, matches={}, errors={})'.format(
            self.astart, self.astop, self.rstart, self.rstop, self.matches, self.errors)

    def __eq__(self, other) -> bool:
        return (
            other.__class__ is self.__class__
            and self.astart == other.astart
            and self.astop == other.astop
            and self.rstart == other.rstart
            and self.rstop == other.rstop
            and self.matches == other.matches
            and self.errors == other.errors
            and self.adapter is other.adapter
            and self.sequence == other.sequence
        )

    def wildcards(self, wildcard_char: str = "N") -> str:
        """
        Return a string that contains, for each wildcard character,
        the character that it matches. For example, if the adapter
        ATNGNA matches ATCGTA, then the string 'CT' is returned.

        If there are indels, this is not reliable as the full alignment
        is not available.
        """
        wildcards = [self.sequence[self.rstart + i] for i in range(self.length)
            if self.adapter.sequence[self.astart + i] == wildcard_char and
                self.rstart + i < len(self.sequence)]
        return ''.join(wildcards)

    def get_info_records(self, read) -> List[List]:
        seq = read.sequence
        qualities = read.qualities
        info = [
            "",
            self.errors,
            self.rstart,
            self.rstop,
            seq[0:self.rstart],
            seq[self.rstart:self.rstop],
            seq[self.rstop:],
            self.adapter.name,
        ]
        if qualities:
            info += [
                qualities[0:self.rstart],
                qualities[self.rstart:self.rstop],
                qualities[self.rstop:],
            ]
        else:
            info += ["", "", ""]

        return [info]

    @abstractmethod
    def removed_sequence_length(self) -> int:
        pass


class RemoveBeforeMatch(SingleMatch):
    """A match that removes sequence before the match"""

    def __repr__(self):
        return 'RemoveBeforeMatch(astart={}, astop={}, rstart={}, rstop={}, matches={}, errors={})'.format(
            self.astart, self.astop, self.rstart, self.rstop, self.matches, self.errors)

    def rest(self) -> str:
        """
        Return the part of the read before this match if this is a
        'front' (5') adapter,
        return the part after the match if this is not a 'front' adapter (3').
        This can be an empty string.
        """
        return self.sequence[:self.rstart]

    def remainder_interval(self) -> Tuple[int, int]:
        """
        Return an interval (start, stop) that describes the part of the read that would
        remain after trimming
        """
        return self.rstop, len(self.sequence)

    def retained_adapter_interval(self) -> Tuple[int, int]:
        return self.rstart, len(self.sequence)

    def trim_slice(self):
        # Same as remainder_interval, but as a slice() object
        return slice(self.rstop, None)

    def trimmed(self, read):
        return read[self.rstop:]

    def removed_sequence_length(self) -> int:
        return self.rstop


class RemoveAfterMatch(SingleMatch):
    """A match that removes sequence after the match"""

    def __repr__(self):
        return "RemoveAfterMatch(astart={}, astop={}, rstart={}, rstop={}, matches={}, errors={})".format(
            self.astart, self.astop, self.rstart, self.rstop, self.matches, self.errors)

    def rest(self) -> str:
        """
        Return the part of the read before this match if this is a
        'front' (5') adapter,
        return the part after the match if this is not a 'front' adapter (3').
        This can be an empty string.
        """
        return self.sequence[self.rstop:]

    def remainder_interval(self) -> Tuple[int, int]:
        """
        Return an interval (start, stop) that describes the part of the read that would
        remain after trimming
        """
        return 0, self.rstart

    def retained_adapter_interval(self) -> Tuple[int, int]:
        return 0, self.rstop

    def trim_slice(self):
        # Same as remainder_interval, but as a slice() object
        return slice(None, self.rstart)

    def trimmed(self, read):
        return read[:self.rstart]

    def adjacent_base(self) -> str:
        return self.sequence[self.rstart - 1:self.rstart]

    def removed_sequence_length(self) -> int:
        return len(self.sequence) - self.rstart


def _generate_adapter_name(_start=[1]) -> str:
    name = str(_start[0])
    _start[0] += 1
    return name


class Matchable(ABC):
    """Something that has a match_to() method."""

    def __init__(self, name: str, *args, **kwargs):
        self.name = name

    @abstractmethod
    def enable_debug(self):
        pass

    @abstractmethod
    def match_to(self, sequence: str):
        pass


class Adapter(Matchable, ABC):

    description = "adapter with one component"  # this is overriden in subclasses

    @abstractmethod
    def spec(self) -> str:
        """Return string representation of this adapter"""

    @abstractmethod
    def create_statistics(self) -> AdapterStatistics:
        pass

    @abstractmethod
    def descriptive_identifier(self) -> str:
        pass


class SingleAdapter(Adapter, ABC):
    """
    This class can find a single adapter characterized by sequence, error rate,
    type etc. within reads.

    where --  A Where enum value. This influences where the adapter is allowed to appear within the
        read.

    sequence -- The adapter sequence as string. Will be converted to uppercase.
        Also, Us will be converted to Ts.

    max_errors -- Maximum allowed errors (non-negative float). If the values is less than 1, this is
        interpreted as a rate directly and passed to the aligner. If it is 1 or greater, the value
        is converted to a rate by dividing it by the length of the sequence.

        The error rate is the number of errors in the alignment divided by the length
        of the part of the alignment that matches the adapter.

    minimum_overlap -- Minimum length of the part of the alignment
        that matches the adapter.

    read_wildcards -- Whether IUPAC wildcards in the read are allowed.

    adapter_wildcards -- Whether IUPAC wildcards in the adapter are
        allowed.

    name -- optional name of the adapter. If not provided, the name is set to a
        unique number.
    """

    allows_partial_matches: bool = True

    def __init__(
        self,
        sequence: str,
        max_errors: float = 0.1,
        min_overlap: int = 3,
        read_wildcards: bool = False,
        adapter_wildcards: bool = True,
        name: Optional[str] = None,
        indels: bool = True,
    ):
        self.name: str = _generate_adapter_name() if name is None else name
        super().__init__(self.name)
        self._debug: bool = False
        self.sequence: str = sequence.upper().replace("U", "T")
        if not self.sequence:
            raise ValueError("Adapter sequence is empty")
        if max_errors >= 1:
            max_errors /= len(self.sequence)
        self.max_error_rate: float = max_errors
        self.min_overlap: int = min(min_overlap, len(self.sequence))
        iupac = frozenset('ABCDGHKMNRSTUVWXY')
        if adapter_wildcards and not set(self.sequence) <= iupac:
            for c in self.sequence:
                if c not in iupac:
                    if c == "I":
                        extra = "For inosine, consider using N instead and please comment " \
                                "on <https://github.com/marcelm/cutadapt/issues/546>."
                    else:
                        extra = "Use only characters 'ABCDGHKMNRSTUVWXY'."
                    raise InvalidCharacter(
                        f"Character '{c}' in adapter sequence '{self.sequence}' is "
                        f"not a valid IUPAC code. {extra}"
                    )
        # Optimization: Use non-wildcard matching if only ACGT is used
        self.adapter_wildcards: bool = adapter_wildcards and not set(self.sequence) <= set("ACGT")
        self.read_wildcards: bool = read_wildcards
        self.indels: bool = indels
        self.aligner = self._aligner()

    def _make_aligner(self, flags: int) -> align.Aligner:
        # TODO
        # Indels are suppressed by setting their cost very high, but a different algorithm
        # should be used instead.
        indel_cost = 1 if self.indels else 100000
        return align.Aligner(
            self.sequence,
            self.max_error_rate,
            flags=flags,
            wildcard_ref=self.adapter_wildcards,
            wildcard_query=self.read_wildcards,
            indel_cost=indel_cost,
            min_overlap=self.min_overlap,
        )

    def __repr__(self):
        return '<{cls}(name={name!r}, sequence={sequence!r}, '\
            'max_error_rate={max_error_rate}, min_overlap={min_overlap}, '\
            'read_wildcards={read_wildcards}, '\
            'adapter_wildcards={adapter_wildcards}, '\
            'indels={indels})>'.format(cls=self.__class__.__name__, **vars(self))

    @property
    def effective_length(self) -> int:
        return self.aligner.effective_length

    def enable_debug(self) -> None:
        """
        Print out the dynamic programming matrix after matching a read to an
        adapter.
        """
        self._debug = True
        self.aligner.enable_debug()

    @abstractmethod
    def _aligner(self):
        pass

    @abstractmethod
    def match_to(self, sequence: str):
        """
        Attempt to match this adapter to the given string.

        Return a Match instance if a match was found;
        return None if no match was found given the matching criteria (minimum
        overlap length, maximum error rate).
        """

    def __len__(self) -> int:
        return len(self.sequence)


class FrontAdapter(SingleAdapter):
    """A 5' adapter"""

    description = "regular 5'"

    def __init__(self, *args, **kwargs):
        self._force_anywhere = kwargs.pop("force_anywhere", False)
        super().__init__(*args, **kwargs)

    def descriptive_identifier(self) -> str:
        return "regular_five_prime"

    def _aligner(self) -> align.Aligner:
        return self._make_aligner(Where.ANYWHERE.value if self._force_anywhere else Where.FRONT.value)

    def match_to(self, sequence: str):
        """
        Attempt to match this adapter to the given read.

        Return a Match instance if a match was found;
        return None if no match was found given the matching criteria (minimum
        overlap length, maximum error rate).
        """
        alignment: Optional[Tuple[int, int, int, int, int, int]] = self.aligner.locate(sequence)
        if self._debug:
            print(self.aligner.dpmatrix)
        if alignment is None:
            return None
        return RemoveBeforeMatch(*alignment, adapter=self, sequence=sequence)

    def spec(self) -> str:
        return f"{self.sequence}..."

    def create_statistics(self) -> FrontAdapterStatistics:
        return FrontAdapterStatistics(self)


class BackAdapter(SingleAdapter):
    """A 3' adapter"""

    description = "regular 3'"

    def __init__(self, *args, **kwargs):
        self._force_anywhere = kwargs.pop("force_anywhere", False)
        super().__init__(*args, **kwargs)

    def descriptive_identifier(self) -> str:
        return "regular_three_prime"

    def _aligner(self):
        return self._make_aligner(Where.ANYWHERE.value if self._force_anywhere else Where.BACK.value)

    def match_to(self, sequence: str):
        """
        Attempt to match this adapter to the given read.

        Return a Match instance if a match was found;
        return None if no match was found given the matching criteria (minimum
        overlap length, maximum error rate).
        """
        alignment: Optional[Tuple[int, int, int, int, int, int]] = self.aligner.locate(sequence)
        if self._debug:
            print(self.aligner.dpmatrix)  # pragma: no cover
        if alignment is None:
            return None
        return RemoveAfterMatch(*alignment, adapter=self, sequence=sequence)

    def spec(self) -> str:
        return f"{self.sequence}"

    def create_statistics(self) -> BackAdapterStatistics:
        return BackAdapterStatistics(self)


class AnywhereAdapter(SingleAdapter):
    """
    An adapter that can be 5' or 3'. If a match involves the first base of
    the read, it is assumed to be a 5' adapter and a 3' otherwise.
    """

    description = "variable 5'/3'"

    def descriptive_identifier(self) -> str:
        return "anywhere"

    def _aligner(self):
        return self._make_aligner(Where.ANYWHERE.value)

    def match_to(self, sequence: str):
        """
        Attempt to match this adapter to the given string.

        Return a Match instance if a match was found;
        return None if no match was found given the matching criteria (minimum
        overlap length, maximum error rate).
        """
        alignment = self.aligner.locate(sequence.upper())
        if self._debug:
            print(self.aligner.dpmatrix)
        if alignment is None:
            return None
        # guess: if alignment starts at pos 0, it’s a 5' adapter
        if alignment[2] == 0:  # index 2 is rstart
            match = RemoveBeforeMatch(*alignment, adapter=self, sequence=sequence)  # type: ignore
        else:
            match = RemoveAfterMatch(*alignment, adapter=self, sequence=sequence)  # type: ignore
        return match

    def spec(self) -> str:
        return f"...{self.sequence}..."

    def create_statistics(self) -> AnywhereAdapterStatistics:
        return AnywhereAdapterStatistics(self)


class NonInternalFrontAdapter(FrontAdapter):
    """A non-internal 5' adapter"""

    description = "non-internal 5'"

    def descriptive_identifier(self) -> str:
        return "noninternal_five_prime"

    def _aligner(self):
        return self._make_aligner(Where.FRONT_NOT_INTERNAL.value)

    def match_to(self, sequence: str):
        # The locate function takes care of uppercasing the sequence
        alignment = self.aligner.locate(sequence)
        if self._debug:
            try:
                print(self.aligner.dpmatrix)
            except AttributeError:
                pass
        if alignment is None:
            return None
        return RemoveBeforeMatch(*alignment, adapter=self, sequence=sequence)  # type: ignore

    def spec(self) -> str:
        return f"X{self.sequence}..."


class NonInternalBackAdapter(BackAdapter):
    """A non-internal 3' adapter"""

    description = "non-internal 3'"

    def descriptive_identifier(self) -> str:
        return "noninternal_three_prime"

    def _aligner(self):
        return self._make_aligner(Where.BACK_NOT_INTERNAL.value)

    def match_to(self, sequence: str):
        # The locate function takes care of uppercasing the sequence
        alignment = self.aligner.locate(sequence)
        if self._debug:
            try:
                print(self.aligner.dpmatrix)  # pragma: no cover
            except AttributeError:
                pass
        if alignment is None:
            return None
        return RemoveAfterMatch(*alignment, adapter=self, sequence=sequence)  # type: ignore

    def spec(self) -> str:
        return f"{self.sequence}X"


class PrefixAdapter(NonInternalFrontAdapter):
    """An anchored 5' adapter"""

    description = "anchored 5'"
    allows_partial_matches = False

    def __init__(self, sequence: str, *args, **kwargs):
        kwargs["min_overlap"] = len(sequence)
        super().__init__(sequence, *args, **kwargs)

    def descriptive_identifier(self) -> str:
        return "anchored_five_prime"

    def _aligner(self):
        if not self.indels:  # TODO or if error rate allows 0 errors anyway
            return align.PrefixComparer(
                self.sequence,
                self.max_error_rate,
                wildcard_ref=self.adapter_wildcards,
                wildcard_query=self.read_wildcards,
                min_overlap=self.min_overlap
            )
        else:
            return self._make_aligner(Where.PREFIX.value)

    def spec(self) -> str:
        return f"^{self.sequence}..."


class SuffixAdapter(NonInternalBackAdapter):
    """An anchored 3' adapter"""

    description = "anchored 3'"
    allows_partial_matches = False

    def __init__(self, sequence: str, *args, **kwargs):
        kwargs["min_overlap"] = len(sequence)
        super().__init__(sequence, *args, **kwargs)

    def descriptive_identifier(self) -> str:
        return "anchored_three_prime"

    def _aligner(self):
        if not self.indels:  # TODO or if error rate allows 0 errors anyway
            return align.SuffixComparer(
                self.sequence,
                self.max_error_rate,
                wildcard_ref=self.adapter_wildcards,
                wildcard_query=self.read_wildcards,
                min_overlap=self.min_overlap
            )
        else:
            return self._make_aligner(Where.SUFFIX.value)

    def spec(self) -> str:
        return f"{self.sequence}$"


class LinkedMatch(Match):
    """
    Represent a match of a LinkedAdapter
    """
    def __init__(self, front_match: RemoveBeforeMatch, back_match: RemoveAfterMatch, adapter: "LinkedAdapter"):
        assert front_match is not None or back_match is not None
        self.front_match: RemoveBeforeMatch = front_match
        self.back_match: RemoveAfterMatch = back_match
        self.adapter: LinkedAdapter = adapter

    def __repr__(self):
        return '<LinkedMatch(front_match={!r}, back_match={}, adapter={})>'.format(
            self.front_match, self.back_match, self.adapter)

    @property
    def matches(self):
        """Number of matching bases"""
        m = 0
        if self.front_match is not None:
            m += self.front_match.matches
        if self.back_match is not None:
            m += self.back_match.matches
        return m

    @property
    def errors(self):
        e = 0
        if self.front_match is not None:
            e += self.front_match.errors
        if self.back_match is not None:
            e += self.back_match.errors
        return e

    def trimmed(self, read):
        if self.front_match:
            read = self.front_match.trimmed(read)
        if self.back_match:
            read = self.back_match.trimmed(read)
        return read

    def remainder_interval(self) -> Tuple[int, int]:
        matches = [match for match in [self.front_match, self.back_match] if match is not None]
        return remainder(matches)

    def retained_adapter_interval(self) -> Tuple[int, int]:
        if self.front_match:
            start = self.front_match.rstart
            offset = self.front_match.rstop
        else:
            start = offset = 0
        if self.back_match:
            end = self.back_match.rstop + offset
        else:
            end = len(self.front_match.sequence)
        return start, end

    def get_info_records(self, read) -> List[List]:
        records = []
        for match, namesuffix in [
            (self.front_match, ";1"),
            (self.back_match, ";2"),
        ]:
            if match is None:
                continue
            record = match.get_info_records(read)[0]
            record[7] = ("none" if self.adapter.name is None else self.adapter.name) + namesuffix
            records.append(record)
            read = match.trimmed(read)
        return records


class LinkedAdapter(Adapter):
    """A 5' adapter combined with a 3' adapter"""

    description = "linked"

    def __init__(
        self,
        front_adapter: SingleAdapter,
        back_adapter: SingleAdapter,
        front_required: bool,
        back_required: bool,
        name: str,
    ):
        super().__init__(name)
        self.front_required = front_required
        self.back_required = back_required

        # The following attributes are needed for the report
        self.where = Where.LINKED
        self.name = _generate_adapter_name() if name is None else name
        self.front_adapter = front_adapter
        self.front_adapter.name = self.name
        self.back_adapter = back_adapter

    def descriptive_identifier(self) -> str:
        return "linked"

    def enable_debug(self):
        self.front_adapter.enable_debug()
        self.back_adapter.enable_debug()

    def match_to(self, sequence: str) -> Optional[LinkedMatch]:
        """
        Match the two linked adapters against a string
        """
        front_match = self.front_adapter.match_to(sequence)
        if self.front_required and front_match is None:
            return None
        if front_match is not None:
            sequence = sequence[front_match.trim_slice()]
        back_match = self.back_adapter.match_to(sequence)
        if back_match is None and (self.back_required or front_match is None):
            return None
        return LinkedMatch(front_match, back_match, self)

    def create_statistics(self) -> LinkedAdapterStatistics:
        return LinkedAdapterStatistics(self, front=self.front_adapter, back=self.back_adapter)

    @property
    def sequence(self):
        return self.front_adapter.sequence + "..." + self.back_adapter.sequence

    @property
    def remove(self):
        return None

    def spec(self) -> str:
        return f"{self.front_adapter.spec()}...{self.back_adapter.spec()}"


class MultipleAdapters(Matchable):
    """
    Represent multiple adapters at once
    """
    def __init__(self, adapters: Sequence[Matchable]):
        super().__init__(name="multiple_adapters")
        self._adapters = adapters

    def enable_debug(self):
        for a in self._adapters:
            a.enable_debug()

    def __getitem__(self, item):
        return self._adapters[item]

    def __len__(self):
        return len(self._adapters)

    def match_to(self, sequence: str) -> Optional[SingleMatch]:
        """
        Find the adapter that best matches the sequence.

        Return either a Match instance or None if there are no matches.
        """
        best_match = None
        for adapter in self._adapters:
            match = adapter.match_to(sequence)
            if match is None:
                continue

            # the no. of matches determines which adapter fits best
            if best_match is None or match.matches > best_match.matches or (
                match.matches == best_match.matches and match.errors < best_match.errors
            ):
                best_match = match
        return best_match


class IndexedAdapters(Matchable, ABC):
    """
    Represent multiple adapters of the same type at once and use an index data structure
    to speed up matching. This acts like a "normal" Adapter as it provides a match_to
    method, but is faster with lots of adapters.

    There are quite a few restrictions:
    - the error rate allows at most 2 mismatches
    - wildcards in the adapter are not allowed
    - wildcards in the read are not allowed

    Use the is_acceptable() method to check individual adapters.
    """
    AdapterIndex = Dict[str, Tuple[SingleAdapter, int, int]]

    def __init__(self, adapters):
        """All given adapters must be of the same type"""
        super().__init__(name="indexed_adapters")
        if not adapters:
            raise ValueError("Adapter list is empty")
        for adapter in adapters:
            self._accept(adapter)
        self._adapters = adapters
        self._multiple_adapters = MultipleAdapters(adapters)
        self._lengths, self._index = self._make_index()
        logger.debug("String lengths in the index: %s", sorted(self._lengths, reverse=True))
        if len(self._lengths) == 1:
            self._length = self._lengths[0]
            self.match_to = self._match_to_one_length
        else:
            self.match_to = self._match_to_multiple_lengths
        self._make_affix = self._get_make_affix()

    def __repr__(self):
        return f"{self.__class__.__name__}(adapters={self._adapters!r})"

    def match_to(self, sequence: str):
        """Never called because it gets overwritten in __init__"""

    @abstractmethod
    def _get_make_affix(self):
        pass

    @abstractmethod
    def _make_match(self, adapter, length, matches, errors, sequence) -> SingleMatch:
        pass

    @classmethod
    def _accept(cls, adapter):
        """Raise a ValueError if the adapter is not acceptable"""
        if adapter.read_wildcards:
            raise ValueError("Wildcards in the read not supported")
        if adapter.adapter_wildcards:
            raise ValueError("Wildcards in the adapter not supported")
        k = int(len(adapter) * adapter.max_error_rate)
        if k > 2:
            raise ValueError("Error rate too high")

    @classmethod
    def is_acceptable(cls, adapter):
        """
        Return whether this adapter is acceptable for being used in an index

        Adapters are not acceptable if they allow wildcards, allow too many errors,
        or would lead to a very large index.
        """
        try:
            cls._accept(adapter)
        except ValueError:
            return False
        return True

    def _make_index(self) -> Tuple[List[int], "AdapterIndex"]:
        logger.info('Building index of %s adapters ...', len(self._adapters))
        index: Dict[str, Tuple[SingleAdapter, int, int]] = dict()
        lengths = set()
        has_warned = False
        for adapter in self._adapters:
            sequence = adapter.sequence
            k = int(adapter.max_error_rate * len(sequence))
            environment = align.edit_environment if adapter.indels else align.hamming_environment
            for s, errors, matches in environment(sequence, k):
                if s in index:
                    other_adapter, other_errors, other_matches = index[s]
                    if matches < other_matches:
                        continue
                    if other_matches == matches and not has_warned:
                        logger.warning(
                            "Adapters %s %r and %s %r are very similar. At %s allowed errors, "
                            "the sequence %r cannot be assigned uniquely because the number of "
                            "matches is %s compared to both adapters.",
                            other_adapter.name, other_adapter.sequence, adapter.name,
                            adapter.sequence, k, s, matches
                        )
                        has_warned = True
                else:
                    index[s] = (adapter, errors, matches)
                lengths.add(len(s))
        logger.info('Built an index containing %s strings.', len(index))

        return sorted(lengths, reverse=True), index

    def _match_to_one_length(self, sequence: str):
        """
        Match the adapters against a string and return a Match that represents
        the best match or None if no match was found
        """
        affix = self._make_affix(sequence.upper(), self._length)
        if "N" in affix:
            # Fall back to non-indexed matching
            return self._multiple_adapters.match_to(sequence)
        try:
            adapter, e, m = self._index[affix]
        except KeyError:
            return None
        return self._make_match(adapter, self._length, m, e, sequence)

    def _match_to_multiple_lengths(self, sequence: str):
        """
        Match the adapters against a string and return a Match that represents
        the best match or None if no match was found
        """
        affix = sequence.upper()

        # Check all the prefixes or suffixes (affixes) that could match
        best_adapter: Optional[SingleAdapter] = None
        best_length = 0
        best_m = -1
        best_e = 1000
        check_n = True
        for length in self._lengths:
            if length < best_m:
                # No chance of getting the same or a higher number of matches, so we can stop early
                break
            affix = self._make_affix(affix, length)
            if check_n:
                if "N" in affix:
                    return self._multiple_adapters.match_to(sequence)
                check_n = False
            try:
                adapter, e, m = self._index[affix]
            except KeyError:
                continue
            if m > best_m or (m == best_m and e < best_e):
                # TODO this could be made to work:
                # assert best_m == -1
                best_adapter = adapter
                best_e = e
                best_m = m
                best_length = length

        if best_m == -1:
            return None
        else:
            return self._make_match(best_adapter, best_length, best_m, best_e, sequence)

    def enable_debug(self):
        pass


class IndexedPrefixAdapters(IndexedAdapters):

    @classmethod
    def _accept(cls, adapter):
        if not isinstance(adapter, PrefixAdapter):
            raise ValueError("Only 5' anchored adapters are allowed")
        return super()._accept(adapter)

    def _make_match(self, adapter, length, matches, errors, sequence):
        return RemoveBeforeMatch(
            astart=0,
            astop=len(adapter.sequence),
            rstart=0,
            rstop=length,
            matches=matches,
            errors=errors,
            adapter=adapter,
            sequence=sequence,
        )

    def _get_make_affix(self):
        return self._make_prefix

    @staticmethod
    def _make_prefix(s, n):
        return s[:n]


class IndexedSuffixAdapters(IndexedAdapters):

    @classmethod
    def _accept(cls, adapter):
        if not isinstance(adapter, SuffixAdapter):
            raise ValueError("Only anchored 3' adapters are allowed")
        return super()._accept(adapter)

    def _make_match(self, adapter, length, matches, errors, sequence):
        return RemoveAfterMatch(
            astart=0,
            astop=len(adapter.sequence),
            rstart=len(sequence) - length,
            rstop=len(sequence),
            matches=matches,
            errors=errors,
            adapter=adapter,
            sequence=sequence,
        )

    def _get_make_affix(self):
        return self._make_suffix

    @staticmethod
    def _make_suffix(s, n):
        return s[-n:]


def warn_duplicate_adapters(adapters):
    d = dict()
    for adapter in adapters:
        key = (adapter.__class__, adapter.sequence)
        if key in d:
            logger.warning("Adapter %r (%s) was specified multiple times! "
                "Please make sure that this is what you want.",
                adapter.sequence, adapter.description)
        d[key] = adapter.name


def remainder(matches: Sequence[Match]) -> Tuple[int, int]:
    """
    Determine which section of the read would not be trimmed. Return a tuple (start, stop)
    that gives the interval of the untrimmed part relative to the original read.

    matches must be non-empty
    """
    if not matches:
        raise ValueError("matches must not be empty")
    start = 0
    for match in matches:
        match_start, match_stop = match.remainder_interval()
        start += match_start
    length = match_stop - match_start
    return (start, start + length)

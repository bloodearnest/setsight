from collections import OrderedDict
import itertools
import os
import re
import subprocess
import tempfile


class RE:

    KEY = re.compile(r'key\s+[-:]\s+([A-G][#b]?)', re.I)
    TIME = re.compile(r'time\s+[-:]\s+(\d+/\d+)', re.I)
    TEMPO = re.compile(r'tempo\s+[-:]\s+(\d+)', re.I)
    SECTION = re.compile(r"""(
        verse|
        chorus|
        bridge|
        pre-chorus|
        instrumental|
        interlude
    )""", re.I | re.VERBOSE)
    # this sucker is a beauty
    CHORD = re.compile(r"""
        ^
        (?P<nochords>[Nn]\.?[Cc]\.?)|       # N.C used for acapella sections
        (?P<note>[A-JZ][♯♭b#]?)             # root note
        (?P<third>mM|min|MIN|Min|maj|MAJ|Maj|m|M)?  # major/minor 3rd
        (?P<fifth>aug|AUG|dim|DIM|\+|ø|°)?  # sharp or flat 5th
        (?P<number>\(?(?:dom|DOM)?\d+\)?)?  # added notes, implies 7th if >7
        (?P<subtraction>\(?no3r?d?\)?)?     # C(no3) = C5
        (?P<altered>[♯♭b#\-\+]\d+)?         # added out-of-chord notes
        (?P<suspension>(?:sus|SUS)\d*)?     # suspended 2 or 4
        (?P<addition>(?:add|ADD|/)\d+)?     # added notes no 7th implied
        (?P<bass>/[A-JZ][♯♭b#]?)?           # BASS
        $
    """, re.VERBOSE)
    CCLI = re.compile(r'CCLI ?Song ?# ?(\d+)', re.I)
    # used to split a chord line into tokens including splitting on |
    CHORD_SPLIT = re.compile(r"""
        [ ](\([^\d].*?\))|  # matches (To SECTION) or (CHORD), keep
        (\|)|                # bar lines, keep
        [ ]+               # spaces, discard
    """, re.VERBOSE)


def convert_pdf(path):
    """Converts a pdf file to text."""
    cmd = ['pdftotext', '-layout', '-enc', 'UTF-8', '-eol', 'unix', '-nopgbrk']
    try:
        _, output = tempfile.mkstemp()
        subprocess.run(cmd + [path, output])
        with open(output, 'r') as f:
            contents = f.read()
        # Some pdf's have zero-width spaces in them
        # TODO: we probably enforce ASCII, stripping unicode?
        contents = contents.replace(u"\u200B", '')
        return contents
    finally:
        os.unlink(output)


def tokenise_chords(chord_line):
    """Tokenise a chord line into separate items.

    Valid tokens are: chords, |, and bracketed directives e.g. (To Chorus).
    """
    chords = []
    chord = []
    closer = None
    brackets = {
        '(': ')',
        '[': ']',
        '{': '}',
    }

    for c in chord_line:
        if closer:
            if c == closer:
                closer = None
            chord.append(c)
        elif c in brackets:
            chord.append(c)
            closer = brackets[c]
        elif c in '| \t\n\r':
            if chord:
                chords.append(''.join(chord))
            if c == '|':
                chords.append('|')
            chord = []
        else:
            chord.append(c)

    if chord:
        chords.append(''.join(chord))

    return chords


def chord_indicies(chord_line):
    """Find the indicies of all chords, bars and comments in the chord line."""
    i = 0
    for chord in tokenise_chords(chord_line):
        search_index = chord_line[i:].find(chord)
        if search_index == -1:
            raise Exception(
                'could not find {} in {}'.format(chord, chord_line)
            )
        chord_index = i + search_index
        yield chord_index, chord
        i = chord_index + len(chord)


def is_chord_line(tokens):
    """Is this line a chord line?"""
    chords = not_chords = 0
    for t in tokens:
        # bars
        if t == '|':
            chords += 1
        elif t[0] == '(' or t[-1] == ')':
            # directions like (To Pre-Chorus) that appear in chord lines
            chords += 1
        elif RE.CHORD.match(t):
            chords += 1
        else:
            not_chords += 1

    return chords > not_chords


def chordpro_line(chord_line, lyric_line):
    """Merge separate chord and lyric lines into one chordpro line.

    Builds an index of the chords, then iterates through the lyric line,
    inserting chords at those indexes. There is some finesse about spaces,
    chords consuming spaces in the lyric line, and handling |'s in chords, as
    well as different chord/lyric line lengths.
    """
    if not chord_line:
        return lyric_line
    elif not lyric_line:
        # chords not wrapped in []
        return chord_line
    else:
        chord_iter = itertools.chain(
            chord_indicies(chord_line),
            itertools.repeat((-1, None)),
        )
        line_iter = enumerate(
            itertools.chain(
                lyric_line,
                itertools.repeat(None),
            ),
        )
        output = []

        index, chord = next(chord_iter)
        i, char = next(line_iter)
        last_char = None
        while chord or char:
            if i == index:
                if chord.startswith('(') and chord.endswith(')'):
                    output.append('{comment:' + chord + '}')
                else:
                    if output and output[-1][-1] == ']':
                        # ensure a space between chords
                        output.append(' ')
                    elif char == ' ' and last_char != ' ':
                        # ensure there is a space in the lyric line to 'attach'
                        # to
                        output.append(' ')
                    output.append('[' + chord + ']')
                # skip up to the chord's length of spaces in the lyric line
                skipped = 0
                while char == ' ' and skipped < len(chord):
                    last_char = char
                    i, char = next(line_iter)
                    skipped += 1

                index, chord = next(chord_iter)
            else:
                if char is None:
                    output.append(' ')
                    last_char = ' '
                else:
                    output.append(char)
                    last_char = char
                i, char = next(line_iter)

        chordpro = ''.join(output)
        # max of 5 spaces
        cleaned = re.sub(r'    +', '    ', chordpro)
        return cleaned.strip()


def fix_superscript_line(superscript, chords):
    """Collapse superscript line into a chord line in the right place.

    This is specific fix for an issue seen with real pdfs. A superscript line
    occurs when pdftotext pushes a numeric superscript into its own line,
    rather than keeping it with the chord.

    E.G. A⁶/B is output as two lines:

    " 6"
    "A /B"

    This function merges back the superscript line into:

    "A6/B"
    """
    out = []
    for s, c in itertools.zip_longest(superscript, chords):
        if s is None:
            out.append(c)
        elif s == ' ':
            out.append(c)
        elif c == ' ':
            out.append(s)
        else:
            # this might be wrong, but best we can do for now
            out.append(s)
            out.append(c)
    return ''.join(out)


def parse_pdf(path, debug):
    """Parse a pdf intro plain text.

    Right now this is simple and a bit brittle. It converts the pdf to text
    using pdttotext from the poppler project, then attempts to parse that
    textual output into a semantic song data.

    In the future, it could parse the pdf directly.
    """

    sheet = convert_pdf(path)

    song = {
        'title': None,
        'key': None,
        'tempo': None,
        'author': None,
        'time': None,
        'ccli': None,
        'legal': '',
        'sections': OrderedDict(),
        'type': 'pdf',
    }

    lines = [l for l in sheet.split('\n') if l.strip()]
    section_name = None
    section_lines = []
    chord_line = None
    superscript_line = None

    for line in lines:

        # assumes everything after a ccli number is blurb
        if song['ccli']:
            song['legal'] += line
            continue
        else:
            ccli = RE.CCLI.search(line)
            if ccli:
                song['ccli'] = ccli.groups()[0]
                song['legal'] += line
                continue

        tokens = tokenise_chords(line)
        if RE.SECTION.match(line):
            if section_name:
                if chord_line:
                    section_lines.append((chord_line, None))
                song['sections'][section_name] = section_lines
            chord_line = None
            section_name = line.strip()
            section_lines = []
        elif is_chord_line(tokens):
            if not section_name:
                section_name = 'VERSE 1'
            if superscript_line is not None:
                chord_line = fix_superscript_line(
                    superscript_line,
                    line.rstrip(),
                )
                superscript_line = None
            else:
                chord_line = line.rstrip()
        elif section_name:
            # handle case where superscript chord markings get pushed onto
            # their own line above by pdftotext
            if line.strip().isdigit():
                superscript_line = line
            else:
                section_lines.append((chord_line, line.rstrip()))
                chord_line = None
        else:
            # preamble
            match = RE.KEY.search(line)
            if match:
                song['key'] = match.groups()[0]
                song['title'] = line[:match.span()[0]].strip()
                continue

            match = RE.TIME.search(line)
            if match:
                song['time'] = match.groups()[0]
                span = match.span()
                tempo_match = RE.TEMPO.search(line)
                if tempo_match:
                    song['tempo'] = tempo_match.groups()[0]
                    span = tempo_match.span()
                song['author'] = line[:span[0]].strip()

    if section_name:
        if chord_line:
            section_lines.append((chord_line, None))
        song['sections'][section_name] = section_lines

    if debug:
        for name, section in song['sections'].items():
            print(name)
            for c, l in section:
                print(c)
                print(l)

    # convert into chordpro
    for name, section_lines in song['sections'].items():
        song['sections'][name] = '\n'.join(
            chordpro_line(c, l) for c, l in section_lines
        )

    if not song['sections']:
        song['type'] = 'pdf-failed'

    return song
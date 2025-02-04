import itertools as it
import re
import sys
from types import MethodType

from manimlib.mobject.svg.svg_mobject import SVGMobject
from manimlib.mobject.types.vectorized_mobject import VMobject
from manimlib.mobject.types.vectorized_mobject import VGroup
from manimlib.utils.iterables import adjacent_pairs
from manimlib.utils.iterables import remove_list_redundancies
from manimlib.utils.tex_file_writing import tex_to_svg_file
from manimlib.utils.tex_file_writing import get_tex_config
from manimlib.utils.tex_file_writing import display_during_execution
from manimlib.logger import log


SCALE_FACTOR_PER_FONT_POINT = 0.001


TEX_HASH_TO_MOB_MAP = {}


def _contains(span_0, span_1):
    return span_0[0] <= span_1[0] and span_1[1] <= span_0[1]


def _get_neighbouring_pairs(iterable):
    return list(adjacent_pairs(iterable))[:-1]


class _PlainTex(SVGMobject):
    CONFIG = {
        "height": None,
        "path_string_config": {
            "should_subdivide_sharp_curves": True,
            "should_remove_null_curves": True,
        },
    }


class _LabelledTex(_PlainTex):
    def __init__(self, file_name=None, **kwargs):
        super().__init__(file_name, **kwargs)
        for glyph in self:
            glyph.glyph_label = _LabelledTex.color_str_to_label(glyph.fill_color)

    @staticmethod
    def color_str_to_label(color_str):
        if len(color_str) == 4:
            # "#RGB" => "#RRGGBB"
            color_str = "#" + "".join([c * 2 for c in color_str[1:]])

        return int(color_str[1:], 16) - 1

    def get_mobjects_from(self, element, style):
        result = super().get_mobjects_from(element, style)
        for mob in result:
            if not hasattr(mob, "glyph_label"):
                mob.glyph_label = -1
        try:
            color_str = element.getAttribute("fill")
            if color_str:
                glyph_label = _LabelledTex.color_str_to_label(color_str)
                for mob in result:
                    mob.glyph_label = glyph_label
        except:
            pass
        return result


class _TexSpan(object):
    def __init__(self, script_type, label):
        # `script_type`: 0 for normal, 1 for subscript, 2 for superscript.
        # Only those spans with `script_type == 0` will be colored.
        self.script_type = script_type
        self.label = label
        self.containing_labels = []

    def __repr__(self):
        return "_TexSpan(" + ", ".join([
            attrib_name + "=" + str(getattr(self, attrib_name))
            for attrib_name in ["script_type", "label", "containing_labels"]
        ]) + ")"


class _TexParser(object):
    def __init__(self, tex_string, additional_substrings):
        self.tex_string = tex_string
        self.tex_spans_dict = {}
        self.specified_substrings = []
        self.current_label = 0
        self.brace_index_pairs = self.get_brace_index_pairs()
        self.add_tex_span((0, len(tex_string)))
        self.break_up_by_double_braces()
        self.break_up_by_scripts()
        self.break_up_by_additional_substrings(additional_substrings)
        self.check_if_overlap()
        self.analyse_containing_labels()
        self.specified_substrings = remove_list_redundancies(self.specified_substrings)

    @staticmethod
    def label_to_color_tuple(rgb):
        # Get a unique color different from black,
        # or the svg file will not include the color information.
        rg, b = divmod(rgb, 256)
        r, g = divmod(rg, 256)
        return r, g, b

    def add_tex_span(self, span_tuple, script_type=0, label=-1):
        if span_tuple in self.tex_spans_dict:
            return

        if script_type == 0:
            # Should be additionally labelled.
            label = self.current_label
            self.current_label += 1

        tex_span = _TexSpan(script_type, label)
        self.tex_spans_dict[span_tuple] = tex_span

    def add_specified_substring(self, span_tuple):
        substring = self.tex_string[slice(*span_tuple)]
        self.specified_substrings.append(substring)

    def get_brace_index_pairs(self):
        result = []
        left_brace_indices = []
        for match_obj in re.finditer(r"(\\*)(\{|\})", self.tex_string):
            # Braces following even numbers of backslashes are counted.
            if len(match_obj.group(1)) % 2 == 1:
                continue
            if match_obj.group(2) == "{":
                left_brace_index = match_obj.span(2)[0]
                left_brace_indices.append(left_brace_index)
            else:
                left_brace_index = left_brace_indices.pop()
                right_brace_index = match_obj.span(2)[1]
                result.append((left_brace_index, right_brace_index))
        if left_brace_indices:
            self.raise_tex_parsing_error("unmatched braces")
        return result

    def break_up_by_double_braces(self):
        # Match paired double braces (`{{...}}`).
        skip_pair = False
        for prev_span_tuple, span_tuple in _get_neighbouring_pairs(
            self.brace_index_pairs
        ):
            if skip_pair:
                skip_pair = False
                continue
            if all([
                span_tuple[0] == prev_span_tuple[0] - 1,
                span_tuple[1] == prev_span_tuple[1] + 1
            ]):
                self.add_tex_span(span_tuple)
                self.add_specified_substring(span_tuple)
                skip_pair = True

    def break_up_by_scripts(self):
        # Match subscripts & superscripts.
        tex_string = self.tex_string
        brace_indices_dict = dict(self.brace_index_pairs)
        for match_obj in re.finditer(r"((?<!\\)(_|\^)\s*)|(\s+(_|\^)\s*)", tex_string):
            script_type = 1 if "_" in match_obj.group() else 2
            token_begin, token_end = match_obj.span()
            if token_end in brace_indices_dict:
                content_span = (token_end, brace_indices_dict[token_end])
            else:
                content_match_obj = re.match(r"\w|\\[a-zA-Z]+", tex_string[token_end:])
                if not content_match_obj:
                    self.raise_tex_parsing_error("unclear subscript/superscript")
                content_span = tuple([
                    index + token_end for index in content_match_obj.span()
                ])
            self.add_tex_span(content_span)
            label = self.tex_spans_dict[content_span].label
            self.add_tex_span(
                (token_begin, content_span[1]),
                script_type=script_type,
                label=label
            )

    def break_up_by_additional_substrings(self, additional_substrings):
        tex_string = self.tex_string
        all_span_tuples = []
        for string in additional_substrings:
            # Only match non-crossing strings.
            for match_obj in re.finditer(re.escape(string), tex_string):
                all_span_tuples.append(match_obj.span())

        script_spans_dict = dict([
            span_tuple[::-1]
            for span_tuple, tex_span in self.tex_spans_dict.items()
            if tex_span.script_type != 0
        ])
        for span_begin, span_end in all_span_tuples:
            if span_end in script_spans_dict.values():
                # Deconstruct spans with subscripts & superscripts.
                while span_end in script_spans_dict:
                    span_end = script_spans_dict[span_end]
                if span_begin >= span_end:
                    continue
            span_tuple = (span_begin, span_end)
            self.add_tex_span(span_tuple)
            self.add_specified_substring(span_tuple)

    def check_if_overlap(self):
        span_tuples = sorted(
            self.tex_spans_dict.keys(),
            key=lambda t: (t[0], -t[1])
        )
        overlapping_span_pairs = []
        for i, span_0 in enumerate(span_tuples):
            for span_1 in span_tuples[i + 1 :]:
                if span_0[1] <= span_1[0]:
                    continue
                if span_0[1] < span_1[1]:
                    overlapping_span_pairs.append((span_0, span_1))
        if overlapping_span_pairs:
            tex_string = self.tex_string
            log.error("Overlapping substring pairs occur in MTex:")
            for span_tuple_pair in overlapping_span_pairs:
                log.error(", ".join(
                    f"\"{tex_string[slice(*span_tuple)]}\""
                    for span_tuple in span_tuple_pair
                ))
            sys.exit(2)

    def analyse_containing_labels(self):
        for span_0, tex_span_0 in self.tex_spans_dict.items():
            if tex_span_0.script_type != 0:
                continue
            for span_1, tex_span_1 in self.tex_spans_dict.items():
                if _contains(span_1, span_0):
                    tex_span_1.containing_labels.append(tex_span_0.label)

    def get_labelled_expression(self):
        tex_string = self.tex_string
        if not self.tex_spans_dict:
            return tex_string

        # Remove the span of extire tex string.
        indices_with_labels = sorted([
            (span_tuple[i], i, span_tuple[1 - i], tex_span.label)
            for span_tuple, tex_span in self.tex_spans_dict.items()
            if tex_span.script_type == 0
            for i in range(2)
        ], key=lambda t: (t[0], -t[1], -t[2]))[1:]

        result = tex_string[: indices_with_labels[0][0]]
        for index_with_label, next_index_with_label in _get_neighbouring_pairs(
            indices_with_labels
        ):
            index, flag, _, label = index_with_label
            next_index, *_ = next_index_with_label
            # Adding one more pair of braces will help maintain the glyghs of tex file...
            if flag == 0:
                color_tuple = _TexParser.label_to_color_tuple(label)
                result += "".join([
                    "{{",
                    "\\color[RGB]",
                    "{",
                    ",".join(map(str, color_tuple)),
                    "}"
                ])
            else:
                result += "}}"
            result += tex_string[index : next_index]
        return result

    def raise_tex_parsing_error(self, message):
        raise ValueError(f"Failed to parse tex ({message}): \"{self.tex_string}\"")


class MTex(VMobject):
    CONFIG = {
        "fill_opacity": 1.0,
        "stroke_width": 0,
        "font_size": 48,
        "alignment": "\\centering",
        "tex_environment": "align*",
        "isolate": [],
        "tex_to_color_map": {},
        "generate_plain_tex_file": False,
    }

    def __init__(self, tex_string, **kwargs):
        super().__init__(**kwargs)
        tex_string = tex_string.strip()
        # Prevent from passing an empty string.
        if not tex_string:
            tex_string = "\\quad"
        self.tex_string = tex_string

        self.generate_mobject()

        self.init_colors()
        self.set_color_by_tex_to_color_map(self.tex_to_color_map)
        self.scale(SCALE_FACTOR_PER_FONT_POINT * self.font_size)

    def get_additional_substrings_to_break_up(self):
        result = remove_list_redundancies([
            *self.tex_to_color_map.keys(), *self.isolate
        ])
        if "" in result:
            result.remove("")
        return result

    def get_parser(self):
        return _TexParser(self.tex_string, self.get_additional_substrings_to_break_up())

    def generate_mobject(self):
        tex_string = self.tex_string
        tex_parser = self.get_parser()
        self.tex_spans_dict = tex_parser.tex_spans_dict
        self.specified_substrings = tex_parser.specified_substrings

        plain_full_tex = self.get_tex_file_body(tex_string)
        plain_hash_val = hash(plain_full_tex)
        if plain_hash_val in TEX_HASH_TO_MOB_MAP:
            self.add(*TEX_HASH_TO_MOB_MAP[plain_hash_val].copy())
            return self

        labelled_expression = tex_parser.get_labelled_expression()
        full_tex = self.get_tex_file_body(labelled_expression)
        hash_val = hash(full_tex)
        if hash_val in TEX_HASH_TO_MOB_MAP and not self.generate_plain_tex_file:
            self.add(*TEX_HASH_TO_MOB_MAP[hash_val].copy())
            return self

        with display_during_execution(f"Writing \"{tex_string}\""):
            filename = tex_to_svg_file(full_tex)
            svg_mob = _LabelledTex(filename)
            self.add(*svg_mob.copy())
            self.build_submobjects()
        TEX_HASH_TO_MOB_MAP[hash_val] = self
        if not self.generate_plain_tex_file:
            return self

        with display_during_execution(f"Writing \"{tex_string}\""):
            filename = tex_to_svg_file(plain_full_tex)
            plain_svg_mob = _PlainTex(filename)
            svg_mob = TEX_HASH_TO_MOB_MAP[hash_val]
            for plain_submob, submob in zip(plain_svg_mob, svg_mob):
                plain_submob.glyph_label = submob.glyph_label
            self.add(*plain_svg_mob.copy())
            self.build_submobjects()
        TEX_HASH_TO_MOB_MAP[plain_hash_val] = self
        return self

    def get_tex_file_body(self, new_tex):
        if self.tex_environment:
            new_tex = "\n".join([
                f"\\begin{{{self.tex_environment}}}",
                new_tex,
                f"\\end{{{self.tex_environment}}}"
            ])
        if self.alignment:
            new_tex = "\n".join([self.alignment, new_tex])

        tex_config = get_tex_config()
        return tex_config["tex_body"].replace(
            tex_config["text_to_replace"],
            new_tex
        )

    def build_submobjects(self):
        if not self.submobjects:
            return
        self.group_submobjects()
        self.sort_scripts_in_tex_order()
        self.assign_submob_tex_strings()

    def group_submobjects(self):
        # Simply pack together adjacent mobjects with the same label.
        new_submobjects = []
        def append_new_submobject(glyphs):
            if glyphs:
                submobject = VGroup(*glyphs)
                submobject.submob_label = glyphs[0].glyph_label
                new_submobjects.append(submobject)

        new_glyphs = []
        current_glyph_label = 0
        for submob in self.submobjects:
            if submob.glyph_label == current_glyph_label:
                new_glyphs.append(submob)
            else:
                append_new_submobject(new_glyphs)
                new_glyphs = [submob]
                current_glyph_label = submob.glyph_label
        append_new_submobject(new_glyphs)
        self.set_submobjects(new_submobjects)

    def sort_scripts_in_tex_order(self):
        # LaTeX always puts superscripts before subscripts.
        # This function sorts the submobjects of scripts in the order of tex given.
        tex_spans_dict = self.tex_spans_dict
        index_and_span_list = sorted([
            (index, span_tuple)
            for span_tuple, tex_span in tex_spans_dict.items()
            if tex_span.script_type != 0
            for index in span_tuple
        ])

        switch_range_pairs = []
        for index_and_span_0, index_and_span_1 in _get_neighbouring_pairs(
            index_and_span_list
        ):
            index_0, span_tuple_0 = index_and_span_0
            index_1, span_tuple_1 = index_and_span_1
            if index_0 != index_1:
                continue
            if not all([
                tex_spans_dict[span_tuple_0].script_type == 1,
                tex_spans_dict[span_tuple_1].script_type == 2
            ]):
                continue
            submob_range_0 = self.range_of_part(
                self.get_part_by_span_tuples([span_tuple_0])
            )
            submob_range_1 = self.range_of_part(
                self.get_part_by_span_tuples([span_tuple_1])
            )
            switch_range_pairs.append((submob_range_0, submob_range_1))

        switch_range_pairs.sort(key=lambda pair: (pair[0].stop, -pair[0].start))
        indices = list(range(len(self.submobjects)))
        for submob_range_0, submob_range_1 in switch_range_pairs:
            indices = [
                *indices[: submob_range_1.start],
                *indices[submob_range_0.start : submob_range_0.stop],
                *indices[submob_range_1.stop : submob_range_0.start],
                *indices[submob_range_1.start : submob_range_1.stop],
                *indices[submob_range_0.stop :]
            ]

        submobs = self.submobjects
        self.set_submobjects([submobs[i] for i in indices])

    def assign_submob_tex_strings(self):
        # Not sure whether this is the best practice...
        # This temporarily supports `TransformMatchingTex`.
        tex_string = self.tex_string
        tex_spans_dict = self.tex_spans_dict
        # Use tex strings including "_", "^".
        label_dict = {}
        for span_tuple, tex_span in tex_spans_dict.items():
            if tex_span.script_type != 0:
                label_dict[tex_span.label] = span_tuple
            else:
                if tex_span.label not in label_dict:
                    label_dict[tex_span.label] = span_tuple

        curr_labels = [submob.submob_label for submob in self.submobjects]
        prev_labels = [curr_labels[-1], *curr_labels[:-1]]
        next_labels = [*curr_labels[1:], curr_labels[0]]
        tex_string_spans = []
        for curr_label, prev_label, next_label in zip(
            curr_labels, prev_labels, next_labels
        ):
            curr_span_tuple = label_dict[curr_label]
            prev_span_tuple = label_dict[prev_label]
            next_span_tuple = label_dict[next_label]
            containing_labels = tex_spans_dict[curr_span_tuple].containing_labels
            tex_string_spans.append([
                prev_span_tuple[1] if prev_label in containing_labels else curr_span_tuple[0],
                next_span_tuple[0] if next_label in containing_labels else curr_span_tuple[1]
            ])
        tex_string_spans[0][0] = label_dict[curr_labels[0]][0]
        tex_string_spans[-1][1] = label_dict[curr_labels[-1]][1]
        for submob, tex_string_span in zip(self.submobjects, tex_string_spans):
            submob.tex_string = tex_string[slice(*tex_string_span)]
            # Support `get_tex()` method here.
            submob.get_tex = MethodType(lambda inst: inst.tex_string, submob)

    def get_part_by_span_tuples(self, span_tuples):
        tex_spans_dict = self.tex_spans_dict
        labels = set(it.chain(*[
            tex_spans_dict[span_tuple].containing_labels
            for span_tuple in span_tuples
        ]))
        return VGroup(*filter(
            lambda submob: submob.submob_label in labels,
            self.submobjects
        ))

    def find_span_components_of_custom_span(self, custom_span_tuple, partial_result=[]):
        span_begin, span_end = custom_span_tuple
        if span_begin == span_end:
            return partial_result
        next_begin_choices = sorted([
            span_tuple[1]
            for span_tuple in self.tex_spans_dict.keys()
            if span_tuple[0] == span_begin and span_tuple[1] <= span_end
        ], reverse=True)
        for next_begin in next_begin_choices:
            result = self.find_span_components_of_custom_span(
                (next_begin, span_end), [*partial_result, (span_begin, next_begin)]
            )
            if result is not None:
                return result
        return None

    def get_part_by_custom_span_tuple(self, custom_span_tuple):
        span_tuples = self.find_span_components_of_custom_span(custom_span_tuple)
        if span_tuples is None:
            tex = self.tex_string[slice(*custom_span_tuple)]
            raise ValueError(f"Failed to get span of tex: \"{tex}\"")
        return self.get_part_by_span_tuples(span_tuples)

    def get_parts_by_tex(self, tex):
        return VGroup(*[
            self.get_part_by_custom_span_tuple(match_obj.span())
            for match_obj in re.finditer(re.escape(tex), self.tex_string)
        ])

    def get_part_by_tex(self, tex, index=0):
        all_parts = self.get_parts_by_tex(tex)
        return all_parts[index]

    def set_color_by_tex(self, tex, color):
        self.get_parts_by_tex(tex).set_color(color)
        return self

    def set_color_by_tex_to_color_map(self, tex_to_color_map):
        for tex, color in list(tex_to_color_map.items()):
            try:
                self.set_color_by_tex(tex, color)
            except:
                pass
        return self

    def indices_of_part(self, part):
        indices = [
            i for i, submob in enumerate(self.submobjects)
            if submob in part
        ]
        if not indices:
            raise ValueError("Failed to find part in tex")
        return indices

    def indices_of_part_by_tex(self, tex, index=0):
        part = self.get_part_by_tex(tex, index=index)
        return self.indices_of_part(part)

    def indices_of_all_parts_by_tex(self, tex, index=0):
        all_parts = self.get_parts_by_tex(tex)
        return list(it.chain(*[
            self.indices_of_part(part) for part in all_parts
        ]))

    def range_of_part(self, part):
        indices = self.indices_of_part(part)
        return range(indices[0], indices[-1] + 1)

    def range_of_part_by_tex(self, tex, index=0):
        part = self.get_part_by_tex(tex, index=index)
        return self.range_of_part(part)

    def index_of_part(self, part):
        return self.indices_of_part(part)[0]

    def index_of_part_by_tex(self, tex, index=0):
        part = self.get_part_by_tex(tex, index=index)
        return self.index_of_part(part)

    def get_tex(self):
        return self.tex_string

    def get_all_isolated_substrings(self):
        tex_string = self.tex_string
        return remove_list_redundancies([
            tex_string[slice(*span_tuple)]
            for span_tuple in self.tex_spans_dict.keys()
        ])

    def list_tex_strings_of_submobjects(self):
        # Work with `index_labels()`.
        log.debug(f"Submobjects of \"{self.get_tex()}\":")
        for i, submob in enumerate(self.submobjects):
            log.debug(f"{i}: \"{submob.get_tex()}\"")


class MTexText(MTex):
    CONFIG = {
        "tex_environment": None,
    }

"""Small safe YAML subset for CamFlow v1.2, Python 3.6 and stdlib only.

Supports indentation mappings/lists, quoted and scalar values, inline JSON-like
lists/maps, block strings, comments, and the workflow YAML emitted by CamFlow.
It deliberately rejects anchors, tags, merge keys, and arbitrary objects.
"""
from __future__ import print_function

import ast
import json
import re


class YamlError(ValueError):
    pass


def _strip_comment(text):
    quote = None
    escaped = False
    out = []
    for char in text:
        if quote:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "#":
            break
        else:
            out.append(char)
    return "".join(out).rstrip()


def _scalar(text):
    text = text.strip()
    if not text:
        return ""
    low = text.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if text[0:1] in ("[", "{"):
        try:
            return json.loads(text)
        except ValueError:
            try:
                return ast.literal_eval(text)
            except (ValueError, SyntaxError):
                raise YamlError("invalid inline value: %s" % text)
    if text[0:1] in ("'", '"'):
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            raise YamlError("invalid quoted scalar: %s" % text)
    if re.match(r"^-?[0-9]+$", text):
        return int(text)
    if re.match(r"^-?(?:[0-9]+\.[0-9]*|[0-9]*\.[0-9]+)(?:[eE][+-]?[0-9]+)?$", text):
        return float(text)
    return text


def _split_key(text):
    quote = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == ":":
            return text[:index].strip(), text[index + 1:].strip()
    raise YamlError("expected mapping key: %s" % text)


def loads(source):
    if not isinstance(source, str):
        raise YamlError("YAML source must be text")
    raw = []
    for number, line in enumerate(source.splitlines(), 1):
        if "\t" in line[:len(line) - len(line.lstrip())]:
            raise YamlError("tabs are not supported (line %d)" % number)
        text = _strip_comment(line)
        if not text.strip() or text.strip() in ("---", "..."):
            continue
        indent = len(text) - len(text.lstrip(" "))
        raw.append((indent, text.strip(), number))
    if not raw:
        return None

    def parse_block(position, indent):
        if position >= len(raw):
            return None, position
        if raw[position][0] != indent:
            raise YamlError("unexpected indentation on line %d" % raw[position][2])
        is_list = raw[position][1].startswith("- ") or raw[position][1] == "-"
        value = [] if is_list else {}
        while position < len(raw) and raw[position][0] == indent:
            _current, text, line = raw[position]
            item = text.startswith("- ") or text == "-"
            if item != is_list:
                raise YamlError("cannot mix list and mapping on line %d" % line)
            position += 1
            if is_list:
                rest = text[1:].strip()
                if not rest:
                    if position >= len(raw) or raw[position][0] <= indent:
                        value.append(None)
                    else:
                        child, position = parse_block(position, raw[position][0])
                        value.append(child)
                elif ":" in rest and not rest.startswith(("'", '"', "[", "{")):
                    key, scalar = _split_key(rest)
                    entry = {key: _scalar(scalar) if scalar else None}
                    if position < len(raw) and raw[position][0] > indent:
                        child_indent = raw[position][0]
                        child, position = parse_block(position, child_indent)
                        if scalar:
                            if not isinstance(child, dict):
                                raise YamlError("list mapping continuation on line %d" % line)
                            entry.update(child)
                        elif child is not None:
                            entry[key] = child
                    value.append(entry)
                else:
                    value.append(_scalar(rest))
            else:
                key, scalar = _split_key(text)
                if not key:
                    raise YamlError("empty mapping key on line %d" % line)
                if scalar in ("|", ">"):
                    lines = []
                    while position < len(raw) and raw[position][0] > indent:
                        child_indent, child_text, _child_line = raw[position]
                        lines.append(" " * max(0, child_indent - indent - 2) + child_text)
                        position += 1
                    value[key] = ("\n" if scalar == "|" else " ").join(lines).rstrip() + "\n"
                elif scalar:
                    value[key] = _scalar(scalar)
                elif position < len(raw) and raw[position][0] > indent:
                    child, position = parse_block(position, raw[position][0])
                    value[key] = child
                else:
                    value[key] = None
        return value, position

    result, position = parse_block(0, raw[0][0])
    if position != len(raw):
        raise YamlError("unparsed YAML near line %d" % raw[position][2])
    return result


def _quote(value):
    if value == "":
        return "''"
    if re.match(r"^-?[0-9]+(?:\.[0-9]+)?$", value):
        return json.dumps(value, ensure_ascii=False)
    if re.match(r"^[A-Za-z0-9_./-]+$", value) and value.lower() not in ("true", "false", "null", "~"):
        return value
    return json.dumps(value, ensure_ascii=False)


def dumps(value, indent=0):
    pad = " " * indent
    if isinstance(value, dict):
        lines = []
        for key in sorted(value.keys()):
            item = value[key]
            if isinstance(item, (dict, list)):
                lines.append("%s%s:" % (pad, key))
                lines.append(dumps(item, indent + 2))
            else:
                lines.append("%s%s: %s" % (pad, key, _dump_scalar(item)))
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append("%s-" % pad)
                lines.append(dumps(item, indent + 2))
            else:
                lines.append("%s- %s" % (pad, _dump_scalar(item)))
        return "\n".join(lines)
    return pad + _dump_scalar(value)


def _dump_scalar(value):
    if value is None: return "null"
    if value is True: return "true"
    if value is False: return "false"
    if isinstance(value, str): return _quote(value)
    if isinstance(value, (int, float)): return str(value)
    return json.dumps(value, ensure_ascii=False)

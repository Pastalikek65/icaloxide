"""RFC 6321 xCal (XML representation of iCalendar) conversion helpers."""

from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET
from typing import Any, Optional

XCAL_NS = "urn:ietf:params:xml:ns:icalendar-2.0"


def _tag(local_name: str) -> str:
    return f"{{{XCAL_NS}}}{local_name}"


def _format_iso_xcal(val: Any) -> tuple[str, str]:
    if isinstance(val, datetime.datetime):
        if val.tzinfo is not None and val.utcoffset() == datetime.timedelta(0):
            return "date-time", val.strftime("%Y-%m-%dT%H:%M:%SZ")
        return "date-time", val.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(val, datetime.date):
        return "date", val.isoformat()
    if isinstance(val, datetime.time):
        if val.tzinfo is not None and val.utcoffset() == datetime.timedelta(0):
            return "time", val.strftime("%H:%M:%SZ")
        return "time", val.strftime("%H:%M:%S")
    if isinstance(val, datetime.timedelta):
        total = int(val.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        buf = [sign, "P"]
        if days:
            buf.append(f"{days}D")
        if hours or mins or secs or not days:
            buf.append("T")
            if hours:
                buf.append(f"{hours}H")
            if mins:
                buf.append(f"{mins}M")
            if secs or (not days and not hours and not mins):
                buf.append(f"{secs}S")
        return "duration", "".join(buf)
    if isinstance(val, bool):
        return "boolean", "true" if val else "false"
    if isinstance(val, int):
        return "integer", str(val)
    if isinstance(val, float):
        return "float", str(val)
    return "text", str(val)


def _append_xcal_prop(props_el: ET.Element, jprop: list[Any]) -> None:
    pname = str(jprop[0]).lower()
    params = dict(jprop[1]) if len(jprop) > 1 and isinstance(jprop[1], dict) else {}
    jtype = str(jprop[2]).lower() if len(jprop) > 2 else "text"
    vals = jprop[3:]

    prop_el = ET.SubElement(props_el, _tag(pname))
    if params:
        params_el = ET.SubElement(prop_el, _tag("parameters"))
        for pk, pv in sorted(params.items()):
            param_el = ET.SubElement(params_el, _tag(str(pk).lower()))
            ptag = "cal-address" if str(pk).lower() in ("member", "delegated-to", "delegated-from", "sent-by") else "text"
            items = pv if isinstance(pv, (list, tuple)) else [pv]
            for item in items:
                val_el = ET.SubElement(param_el, _tag(ptag))
                val_el.text = str(item)

    if jtype == "recur" and vals and isinstance(vals[0], dict):
        rec_el = ET.SubElement(prop_el, _tag("recur"))
        for rk, rv in vals[0].items():
            sub_items = rv if isinstance(rv, (list, tuple)) else [rv]
            for item in sub_items:
                el = ET.SubElement(rec_el, _tag(str(rk).lower()))
                el.text = str(item)
        return

    if pname == "request-status" and vals and isinstance(vals[0], list):
        parts = vals[0]
        if len(parts) >= 1:
            c_el = ET.SubElement(prop_el, _tag("code"))
            c_el.text = str(parts[0])
        if len(parts) >= 2:
            d_el = ET.SubElement(prop_el, _tag("description"))
            d_el.text = str(parts[1])
        if len(parts) >= 3 and str(parts[2]):
            x_el = ET.SubElement(prop_el, _tag("data"))
            x_el.text = str(parts[2])
        return

    if jtype == "period":
        for val in vals:
            s_val = str(val)
            if "/" in s_val:
                start_s, end_or_dur = s_val.split("/", 1)
                per_el = ET.SubElement(prop_el, _tag("period"))
                st_el = ET.SubElement(per_el, _tag("start"))
                st_el.text = start_s
                is_dur = end_or_dur.startswith(("P", "-P", "+P"))
                tag2 = "duration" if is_dur else "end"
                end_el = ET.SubElement(per_el, _tag(tag2))
                end_el.text = end_or_dur
            else:
                val_el = ET.SubElement(prop_el, _tag("period"))
                val_el.text = s_val
        return

    if jtype == "float" and pname == "geo" and vals and isinstance(vals[0], list) and len(vals[0]) == 2:
        lat_el = ET.SubElement(prop_el, _tag("latitude"))
        lat_el.text = str(vals[0][0])
        lon_el = ET.SubElement(prop_el, _tag("longitude"))
        lon_el.text = str(vals[0][1])
        return

    for val in vals:
        if isinstance(val, (datetime.date, datetime.datetime, datetime.timedelta, datetime.time, bool, int, float)):
            xtype, text = _format_iso_xcal(val)
        else:
            xtype, text = jtype, ("true" if val is True else "false" if val is False else str(val))
        val_el = ET.SubElement(prop_el, _tag(xtype))
        val_el.text = text


def _build_xcal_component(parent_el: Optional[ET.Element], jcal_node: list[Any]) -> ET.Element:
    cname = str(jcal_node[0]).lower()
    el = (
        ET.Element(_tag(cname))
        if parent_el is None
        else ET.SubElement(parent_el, _tag(cname))
    )
    jprops = jcal_node[1] if len(jcal_node) > 1 else []
    jsubs = jcal_node[2] if len(jcal_node) > 2 else []
    if jprops:
        props_el = ET.SubElement(el, _tag("properties"))
        for jp in jprops:
            _append_xcal_prop(props_el, jp)
    if jsubs:
        comps_el = ET.SubElement(el, _tag("components"))
        for sub in jsubs:
            _build_xcal_component(comps_el, sub)
    return el


def component_to_xcal(comp: Any) -> str:
    """Serialize any ``compat.Component`` into RFC 6321 xCal XML."""
    ET.register_namespace("", XCAL_NS)
    jcal = comp.to_jcal()
    if str(jcal[0]).lower() == "vcalendar":
        root = ET.Element(_tag("icalendar"))
        _build_xcal_component(root, jcal)
    else:
        root = _build_xcal_component(None, jcal)
    return ET.tostring(root, encoding="unicode")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_xcal_prop(prop_el: ET.Element) -> list[Any]:
    pname = _local_name(prop_el.tag)
    params: dict[str, Any] = {}
    jtype = "text"
    vals: list[Any] = []

    rs_parts: dict[str, str] = {}
    for child in prop_el:
        cname = _local_name(child.tag)
        if cname == "parameters":
            for pel in child:
                pk = _local_name(pel.tag)
                pvals = [(sub.text or "") for sub in pel]
                if not pvals and pel.text:
                    pvals = [pel.text]
                params[pk] = pvals[0] if len(pvals) == 1 else pvals
        elif pname == "request-status" and cname in ("code", "description", "data"):
            rs_parts[cname] = child.text or ""
        elif cname == "period":
            jtype = "period"
            sub_map = {_local_name(sc.tag): (sc.text or "") for sc in child}
            if "start" in sub_map:
                second = sub_map.get("duration", sub_map.get("end", ""))
                vals.append(f"{sub_map['start']}/{second}")
            else:
                vals.append(child.text or "")
        elif cname == "recur":
            jtype = "recur"
            rec: dict[str, Any] = {}
            for rchild in child:
                rk = _local_name(rchild.tag)
                rv: Any = rchild.text or ""
                if rk in ("interval", "count", "bymonth", "bymonthday", "byyearday", "byweekno", "bysetpos", "byhour", "byminute", "bysecond"):
                    try:
                        rv = int(rv)
                    except ValueError:
                        pass
                if rk in rec:
                    existing = rec[rk]
                    rec[rk] = [*existing, rv] if isinstance(existing, list) else [existing, rv]
                else:
                    rec[rk] = rv
            vals.append(rec)
        elif cname in ("latitude", "longitude"):
            jtype = "float"
            vals.append(float(child.text or "0"))
        else:
            jtype = cname
            raw = child.text or ""
            if cname == "integer":
                vals.append(int(raw))
            elif cname == "float":
                vals.append(float(raw))
            elif cname == "boolean":
                vals.append(raw.strip().lower() == "true")
            else:
                vals.append(raw)

    if pname == "request-status" and rs_parts:
        rs_list = [rs_parts.get("code", ""), rs_parts.get("description", "")]
        if "data" in rs_parts:
            rs_list.append(rs_parts["data"])
        return [pname, params, "text", rs_list]
    if pname == "geo" and len(vals) == 2 and all(isinstance(x, (int, float)) for x in vals):
        return [pname, params, "float", [vals[0], vals[1]]]
    if not vals:
        vals = [""]
    return [pname, params, jtype, *vals]


def _xcal_element_to_jcal(el: ET.Element) -> list[Any]:
    cname = _local_name(el.tag)
    jprops: list[list[Any]] = []
    jsubs: list[list[Any]] = []
    for child in el:
        tag = _local_name(child.tag)
        if tag == "properties":
            for prop_el in child:
                jprops.append(_parse_xcal_prop(prop_el))
        elif tag == "components":
            for sub_el in child:
                jsubs.append(_xcal_element_to_jcal(sub_el))
    return [cname, jprops, jsubs]


def xcal_to_jcal(xml_data: Any) -> list[Any]:
    """Parse an RFC 6321 xCal XML string or bytes into an RFC 7265 jCal structure."""
    text = xml_data.decode("utf-8") if isinstance(xml_data, bytes) else str(xml_data)
    root = ET.fromstring(text)
    if _local_name(root.tag) == "icalendar":
        for child in root:
            return _xcal_element_to_jcal(child)
        raise ValueError("empty <icalendar> xCal root element")
    return _xcal_element_to_jcal(root)


__all__ = ["XCAL_NS", "component_to_xcal", "xcal_to_jcal"]

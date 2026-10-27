#!/usr/bin/env python3
"""Shared supported MIME identities and deterministic document-icon metadata."""
import xml.etree.ElementTree as ET

NAMESPACE = 'http://www.freedesktop.org/standards/shared-mime-info'
ET.register_namespace('', NAMESPACE)

MIMES = {
    'writer': ['application/vnd.openxmlformats-officedocument.wordprocessingml.document',
               'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
               'application/vnd.ms-word.document.macroEnabled.12',
               'application/vnd.ms-word.template.macroEnabled.12',
               'application/msword', 'application/msword-template',
               'application/vnd.oasis.opendocument.text', 'application/rtf', 'text/rtf'],
    'sheets': ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
              'application/vnd.ms-excel.sheet.macroEnabled.12',
              'application/vnd.ms-excel.sheet.binary.macroEnabled.12',
              'application/vnd.ms-excel', 'text/csv', 'text/tab-separated-values',
              'application/vnd.oasis.opendocument.spreadsheet'],
    'pdf': ['application/pdf'],
    'slides': ['application/x-deckcraft', 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
               'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
               'application/vnd.openxmlformats-officedocument.presentationml.template'],
}


def document_icon_name(app):
    if app['slug'] not in MIMES:
        raise ValueError('Unknown Office document family')
    return 'greyward-' + app['slug'] + '-document'


def document_mime(app, upstream=None):
    """Retain upstream format detection; change only the supported types' icons.

    Standard formats already exist in shared-mime-info. New entries here carry
    an icon only, never a filename glob, handler association or format claim.
    """
    if upstream is not None:
        root = ET.fromstring(upstream)
        if root.tag != '{' + NAMESPACE + '}mime-info':
            raise ValueError('Unexpected upstream MIME definition')
    else:
        root = ET.Element('{' + NAMESPACE + '}mime-info')
    for mime in MIMES[app['slug']]:
        entries = [entry for entry in root.findall('{' + NAMESPACE + '}mime-type')
                   if entry.get('type') == mime]
        if len(entries) > 1:
            raise ValueError('Duplicate upstream MIME definition: ' + mime)
        entry = entries[0] if entries else ET.SubElement(root, '{' + NAMESPACE + '}mime-type', {'type': mime})
        for icon in entry.findall('{' + NAMESPACE + '}icon'):
            entry.remove(icon)
        ET.SubElement(entry, '{' + NAMESPACE + '}icon', {'name': document_icon_name(app)})
    return ET.tostring(root, encoding='utf-8', xml_declaration=True) + b'\n'

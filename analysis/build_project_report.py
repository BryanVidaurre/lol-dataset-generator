"""Render the authored project report to Word and draw figures from evidence.

Does not preprocess datasets or execute mining. Install requirements-reports.txt.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import duckdb
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
INLINE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)|\*\*([^*]+)\*\*|`([^`]+)`|\*([^*]+)\*')


def example_state(root):
    """Verify and save the real, explicitly selected example used in section 6.3."""
    mid, cutoff = 'EUW1_8009376485', 900000
    event_path = root / 'data/processed/ampliacion_50k_nuevos_parches/events_clean.csv'
    state_path = root / 'data/mining_ready/ampliacion_50k_nuevos_parches/strategic_states.parquet'
    escaped_events = str(event_path).replace("'", "''")
    escaped_states = str(state_path).replace("'", "''")
    db = duckdb.connect()
    try:
        db.execute("SET memory_limit='512MB'")
        rows = db.execute(f"""SELECT event_id,timestamp_ms::BIGINT AS timestamp_ms,
            event_type,actor_team_id::INTEGER AS actor_team_id,monster_type,building_type,
            frame_index::INTEGER AS frame_index,event_index::INTEGER AS event_index
            FROM read_csv('{escaped_events}',all_varchar=true) WHERE match_id='{mid}'
            ORDER BY timestamp_ms,frame_index,event_index""")
        fields = [c[0] for c in rows.description]
        events = [dict(zip(fields, r)) for r in rows.fetchall()]
        for event in events:
            event['feature'] = None
            if event['event_type'] == 'CHAMPION_KILL':
                event['feature'] = 'kills'
            elif event['event_type'] == 'ELITE_MONSTER_KILL' and event['monster_type'] == 'DRAGON':
                event['feature'] = 'dragons'
            elif event['event_type'] == 'BUILDING_KILL' and ('TOWER' in (event['building_type'] or '') or 'TURRET' in (event['building_type'] or '')):
                event['feature'] = 'towers'
        included = [e for e in events if e['timestamp_ms'] <= cutoff and e['feature']]
        counts = Counter((e['feature'], e['actor_team_id']) for e in included)
        future = next(e for e in events if e['timestamp_ms'] > cutoff and e['feature'])
        result = db.execute(f"""SELECT match_id,team_id,patch,split,game_minute,recorded_event_count,
            kills_team,kills_enemy,kills_diff,dragons_team,dragons_enemy,dragons_diff,
            towers_team,towers_enemy,towers_diff FROM read_parquet('{escaped_states}')
            WHERE match_id='{mid}' AND game_minute=15 ORDER BY team_id""")
        fields = [c[0] for c in result.description]
        states = [dict(zip(fields, row)) for row in result.fetchall()]
        if len(states) != 2:
            raise ValueError('Example must have both team perspectives at minute 15')
        for state in states:
            team = state['team_id']
            for feature in ('kills', 'dragons', 'towers'):
                own, enemy = counts[feature, team], counts[feature, 300-team]
                if (state[f'{feature}_team'], state[f'{feature}_enemy'], state[f'{feature}_diff']) != (own, enemy, own-enemy):
                    raise ValueError('Example counters do not match stored strategic state')
        # Keep the authored numerical example and its sources consistent on rebuild.
        if [e['timestamp_ms'] for e in included] != [358687, 364737, 638113, 693885, 734165, 829669, 832074]:
            raise ValueError('Real example changed; update the report instead of silently replacing it')
        if future['timestamp_ms'] != 903404 or states[0]['recorded_event_count'] != 29:
            raise ValueError('Cutoff example changed')
        folder = root / 'reports/informe_preprocesamiento_assets'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'ejemplo_estado_minuto15.json').write_text(json.dumps({
            'match_id': mid, 'cutoff_ms': cutoff, 'selection': 'didactic; low event count; no outcome used',
            'sources': [str(event_path.relative_to(root)), str(state_path.relative_to(root))],
            'relevant_events_at_cutoff': included, 'next_relevant_event_excluded': future,
            'all_exported_events_at_cutoff': sum(e['timestamp_ms'] <= cutoff for e in events),
            'verified_states': states, 'counter_validation': 'passed'}, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        db.close()


def figures(root):
    original_path = root / 'reports/perfilamiento_50k_nuevos_parches/data_review.json'
    validation_path = root / 'data/mining_ready/ampliacion_50k_nuevos_parches/preprocessing_validation.json'
    original = json.loads(original_path.read_text(encoding='utf-8'))
    validation = json.loads(validation_path.read_text(encoding='utf-8'))
    folder = root / 'reports/informe_preprocesamiento_assets'
    folder.mkdir(parents=True, exist_ok=True)
    regions = sorted(original['summary']['regions'])
    initial = [original['summary']['regions'][r] for r in regions]
    retained = [validation['regions'][r] for r in regions]
    assert sum(initial) == validation['counts']['original_matches']
    assert sum(retained) == validation['counts']['matches']
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10,
                         'axes.spines.top': False, 'axes.spines.right': False})
    fig, ax = plt.subplots(figsize=(7, 4))
    xs = list(range(len(regions)))
    ax.bar([x-.19 for x in xs], initial, .38, label='Originales', color='#285b86')
    ax.bar([x+.19 for x in xs], retained, .38, label='Conservadas', color='#d28b35')
    ax.set_xticks(xs, regions)
    ax.set(ylabel='Número de partidas', xlabel='Plataforma')
    ax.legend(frameon=False, ncols=2, loc='lower center', bbox_to_anchor=(.5, 1.02))
    ax.set_ylim(0, max(initial)*1.15)
    ax.grid(axis='y', alpha=.2)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(folder / 'distribucion_regiones.png', dpi=220)
    fig.savefig(folder / 'distribucion_regiones.svg')
    plt.close(fig)
    patches = original['summary']['patches']
    ordered = sorted(patches, key=lambda p: tuple(map(int, p.split('.'))))
    fig, ax = plt.subplots(figsize=(8.5, 4))
    ax.bar(ordered, [patches[p] for p in ordered], color='#285b86')
    ax.tick_params(axis='x', rotation=60, labelsize=8)
    ax.set(ylabel='Número de partidas originales', xlabel='Grupo de parche (texto)')
    ax.grid(axis='y', alpha=.2)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(folder / 'distribucion_parches.png', dpi=220)
    fig.savefig(folder / 'distribucion_parches.svg')
    plt.close(fig)
    (folder / 'fuentes_figuras.json').write_text(json.dumps({
        'sources': [str(original_path.relative_to(root)), str(validation_path.relative_to(root))],
        'regions': dict(zip(regions, initial)), 'regions_retained': dict(zip(regions, retained)),
        'patch_groups_original': {p: patches[p] for p in ordered}},
        ensure_ascii=False, indent=2), encoding='utf-8')


def inline(paragraph, text):
    pos = 0
    for match in INLINE.finditer(text):
        paragraph.add_run(text[pos:match.start()])
        link, target, bold, code, italic = match.groups()
        if link is not None:
            hyperlink = OxmlElement('w:hyperlink')
            hyperlink.set(qn('r:id'), paragraph.part.relate_to(target, RT.HYPERLINK, is_external=True))
            run = OxmlElement('w:r')
            props = OxmlElement('w:rPr')
            style = OxmlElement('w:rStyle')
            style.set(qn('w:val'), 'Hyperlink')
            props.append(style)
            run.append(props)
            content = OxmlElement('w:t')
            content.text = link
            run.append(content)
            hyperlink.append(run)
            paragraph._p.append(hyperlink)
        else:
            run = paragraph.add_run(bold or code or italic)
            run.bold = bold is not None
            run.italic = italic is not None
            if code is not None:
                run.font.name = 'Consolas'
                run.font.size = Pt(9)
        pos = match.end()
    paragraph.add_run(text[pos:])


def styles(doc):
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2.5)
    section.left_margin = section.right_margin = Cm(2.5)
    normal = doc.styles['Normal']
    normal.font.name, normal.font.size = 'Times New Roman', Pt(11)
    normal.paragraph_format.line_spacing = 1.15
    normal.paragraph_format.space_after = Pt(6)
    language = OxmlElement('w:lang')
    language.set(qn('w:val'), 'es-CL')
    normal.element.get_or_add_rPr().append(language)
    for name, size in [('Title', 22), ('Heading 1', 15), ('Heading 2', 12)]:
        style = doc.styles[name]
        style.font.name = 'Times New Roman'
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string('203C54')
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.space_before = Pt(14)
    header = section.header.paragraphs[0]
    run = header.add_run('Proyecto de Minería de Datos · League of Legends')
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor.from_string('666666')
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.add_run('Página ')
    field = OxmlElement('w:fldSimple')
    field.set(qn('w:instr'), 'PAGE')
    footer._p.append(field)
    doc.core_properties.title = 'Preparación y validación de datos de League of Legends'
    doc.core_properties.subject = 'Informe de preprocesamiento para minería de datos'
    doc.core_properties.author = ''
    doc.core_properties.language = 'es-CL'
    doc.core_properties.modified = datetime.now(timezone.utc)


def table(doc, lines):
    cells = [[c.strip() for c in line.strip().strip('|').split('|')] for line in lines]
    cells = [row for row in cells if not all(re.fullmatch(r':?-+:?', c) for c in row)]
    columns = len(cells[0])
    result = doc.add_table(rows=0, cols=columns)
    result.style = 'Table Grid'
    result.autofit = False
    for column in result.columns:
        column.width = Cm(16 / columns)
    for index, values in enumerate(cells):
        if len(values) != columns:
            raise ValueError('Invalid Markdown table width')
        row = result.add_row()
        for cell, value in zip(row.cells, values):
            inline(cell.paragraphs[0], value)
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(3)
                paragraph.paragraph_format.space_before = Pt(3)
                for run in paragraph.runs:
                    run.font.size = Pt(9)
                    if index == 0:
                        run.bold = True
            if index == 0:
                shade = OxmlElement('w:shd')
                shade.set(qn('w:fill'), 'E8EEF3')
                cell._tc.get_or_add_tcPr().append(shade)
        if index == 0:
            repeat = OxmlElement('w:tblHeader')
            row._tr.get_or_add_trPr().append(repeat)
    doc.add_paragraph()


def render(markdown, output):
    text = markdown.read_text(encoding='utf-8')
    if '\ufffd' in text:
        raise ValueError('Replacement characters found in report')
    doc = Document()
    styles(doc)
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith('```'):
            i += 1
            while i < len(lines) and not lines[i].startswith('```'):
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(0)
                run = p.add_run(lines[i])
                run.font.name, run.font.size = 'Consolas', Pt(8)
                i += 1
            i += 1
            continue
        if line.startswith('|'):
            block = []
            while i < len(lines) and lines[i].startswith('|'):
                block.append(lines[i])
                i += 1
            table(doc, block)
            continue
        if match := re.fullmatch(r'!\[([^\]]*)\]\(([^)]+)\)', line):
            doc.add_picture(str(markdown.parent / match.group(2)), width=Cm(16))
        elif line.startswith('# '):
            doc.add_paragraph(line[2:], 'Title')
        elif line.startswith('## '):
            doc.add_heading(line[3:], level=1)
        elif line.startswith('### '):
            doc.add_heading(line[4:], level=2)
        elif line.startswith('- '):
            inline(doc.add_paragraph(style='List Bullet'), line[2:])
        else:
            p = doc.add_paragraph()
            if re.match(r'\d+\. ', line):
                p.paragraph_format.left_indent = Cm(.5)
                p.paragraph_format.first_line_indent = Cm(-.5)
            inline(p, line.rstrip())
        i += 1
    doc.save(output)
    return doc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--markdown', type=Path, default=ROOT / 'reports/informe_proyecto_preprocesamiento.md')
    parser.add_argument('--output', type=Path, help='Optional Word path; useful when a previous version is open')
    args = parser.parse_args()
    example_state(ROOT)
    figures(ROOT)
    output = args.output or args.markdown.with_suffix('.docx')
    document = render(args.markdown, output)
    print(f'Created: {output}')
    print(f'Tables: {len(document.tables)}; figures: {len(document.inline_shapes)}')


if __name__ == '__main__':
    main()

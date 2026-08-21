#!/usr/bin/env python3
"""DISPOSABLE RECON PROBE - builds SANITIZED fixtures for future testing.

Redaction policy:
  * Whole `User` object dropped (contains Username, External_Id/Banner ID,
    Password field, Last_Login -- internal PII over-exposed by the API).
  * *ExternalId (Banner IDs) dropped.
  * Personal names / emails / phones of submitter & approver replaced with
    typed placeholders.
  * Contact* fields replaced with typed placeholders too (may be personal).
  * Announcement Title / Category / dates / body HTML preserved verbatim --
    that is the published public content we must parse faithfully.
  * data: URI image payloads truncated (multi-MB, no test value).
"""
import importlib.util, json, re, os, sys

spec = importlib.util.spec_from_file_location(
    'probe', os.path.join(os.path.dirname(__file__), '02-api-probe.py'))
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)

OUT = '/home/sedlock/src/DailyMail/artifacts/reconnaissance/fixtures'
os.makedirs(OUT, exist_ok=True)

PERSON_FIELDS = {
    'SubmittedByName': '[REDACTED_PERSON_NAME]',
    'SubmittedByEmail': '[REDACTED_EMAIL]',
    'SubmittedByPhone': '[REDACTED_PHONE]',
    'ApprovedByName': '[REDACTED_PERSON_NAME]',
    'ApprovedByEmail': '[REDACTED_EMAIL]',
    'ApprovedByPhone': '[REDACTED_PHONE]',
    'UpdatedByName': '[REDACTED_PERSON_NAME]',
    'ContactName': '[REDACTED_PERSON_NAME]',
    'ContactRowanEmail': '[REDACTED_EMAIL]',
    'ContactPhone': '[REDACTED_PHONE]',
}
DROP_FIELDS = {'SubmittedByExternalId', 'ApproverExternalId', 'SubmittedById',
               'ApprovedById', 'UpdatedById'}

def truncate_data_uris(html):
    return re.sub(r'(data:[a-z/+.-]+;base64,)[A-Za-z0-9+/=]{80,}',
                  r'\1[TRUNCATED_BASE64_IMAGE]', html or '')

def sanitize(rec):
    out = {k: v for k, v in rec.items() if k != 'User'}
    s = dict(out['Submission'])
    for f in DROP_FIELDS:
        s.pop(f, None)
    for f, ph in PERSON_FIELDS.items():
        if f in s and s[f]:
            s[f] = ph
    s['SubmissionBody'] = '[REDACTED_BASE64_DUPLICATE_OF_FullBody]'
    out['Submission'] = s
    out['FullBody'] = truncate_data_uris(out.get('FullBody'))
    out['ShortBody'] = out.get('ShortBody')
    return out

def grab(aud, date):
    st, d = p.home(audience=aud, start_date=date, max_records=500)
    return d['data']

if __name__ == '__main__':
    # 1. Full single-day snapshots for both audiences on the validated date.
    for aud in ('Employees', 'Students'):
        dd = grab(aud, '2026-08-20')
        fx = {
            '_note': 'SANITIZED recon fixture. Personal fields redacted; '
                     'data: URI images truncated. Public announcement content '
                     'preserved verbatim.',
            '_source': 'POST /RowanAnnouncer/screenservices/RowanAnnouncer/'
                       'MainFlow/Home/ActionGetHomeData',
            '_query': {'Audience': aud, 'StartDate': '2026-08-20',
                       'EndDate': '1900-01-01', 'MaxRecords': 500,
                       'StartIndex': 0},
            'TotalCount': dd['TotalCount'],
            'Categories': dd['Categories']['List'],
            'Announcements': [sanitize(a) for a in dd['Announcements']['List']],
        }
        path = f'{OUT}/homedata-{aud.lower()}-2026-08-20.sanitized.json'
        json.dump(fx, open(path, 'w'), indent=1, ensure_ascii=False)
        print(f'wrote {path}  ({os.path.getsize(path)/1024:.1f} KB, '
              f'{len(fx["Announcements"])} records)')

    # 2. Explicit zero-announcement response (validation fixture).
    dd = grab('Employees', '1999-01-01')
    path = f'{OUT}/homedata-empty-day.sanitized.json'
    json.dump({'_note': 'Genuine zero-announcement response. Note Categories '
                        'IS still populated (33 entries) and TotalCount=="0" -- '
                        'this is how a real empty day differs from a broken call.',
               'TotalCount': dd['TotalCount'],
               'Categories': dd['Categories']['List'],
               'Announcements': []}, open(path, 'w'), indent=1)
    print(f'wrote {path}')

    # 3. Broken-call response (bogus apiVersion) for failure-detection tests.
    st, d = p.home(audience='Employees', start_date='2026-08-20',
                   api_version='DELIBERATELY_INVALID')
    path = f'{OUT}/homedata-broken-apiversion.sanitized.json'
    json.dump({'_note': 'Response when apiVersion is stale/invalid. HTTP 200 '
                        'but data=={} and hasApiVersionChanged==true. This is '
                        'the silent-failure mode the collector MUST detect.',
               'response': d}, open(path, 'w'), indent=1)
    print(f'wrote {path}')

    # 4. One event announcement (all event fields populated).
    st, d = p.home(audience='Employees', start_date='2026-01-01',
                   end_date='2026-12-31', max_records=2000)
    evs = [a for a in d['data']['Announcements']['List']
           if a['Submission'].get('Event') and a['Submission'].get('EventEndTime')
           not in ('', '00:00:00')]
    if evs:
        path = f'{OUT}/announcement-event.sanitized.json'
        json.dump({'_note': 'Event announcement with event fields populated.',
                   'record': sanitize(evs[0])}, open(path, 'w'), indent=1,
                  ensure_ascii=False)
        print(f'wrote {path}  (SubmissionId {evs[0]["Submission"]["Id"]})')

    # 5. Student-only announcement.
    st, d = p.home(audience='Students', start_date='2026-01-01',
                   end_date='2026-12-31', max_records=2000)
    so = [a for a in d['data']['Announcements']['List']
          if a['Submission']['Audience'] == 'Students']
    if so:
        path = f'{OUT}/announcement-student-only.sanitized.json'
        json.dump({'_note': 'Student-only announcement (Audience=="Students").',
                   'record': sanitize(so[0])}, open(path, 'w'), indent=1,
                  ensure_ascii=False)
        print(f'wrote {path}  (SubmissionId {so[0]["Submission"]["Id"]})')

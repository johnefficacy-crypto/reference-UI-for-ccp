update subjects
set subject_group = 'social_science',
    metadata = '{"exams":["nabard"]}'::jsonb
where slug = 'economic-social-issues';
select id, slug, name, subject_group, metadata from subjects where slug in ('economic-social-issues','agriculture-rural-development','computer-knowledge','decision-making') order by slug;
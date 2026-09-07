insert into subjects (slug, name, subject_group, metadata)
values ('agriculture-rural-development', 'Agriculture and Rural Development', 'social_science', '{"exams":["nabard"]}'::jsonb)
on conflict (slug) do nothing;
select id, slug, name, subject_group, metadata from subjects where slug in ('economic-social-issues','agriculture-rural-development');
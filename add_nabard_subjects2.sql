insert into subjects (slug, name, subject_group, metadata)
values
  ('economic-social-issues', 'Economic and Social Issues', 'social_science', '{"exams":["nabard"]}'::jsonb),
  ('agriculture-rural-development', 'Agriculture and Rural Development', 'social_science', '{"exams":["nabard"]}'::jsonb);
select id, slug, name, subject_group from subjects where slug in ('economic-social-issues','agriculture-rural-development');

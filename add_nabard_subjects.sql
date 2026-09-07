begin;
insert into subjects (slug, name, subject_group)
values ('computer-knowledge', 'Computer Knowledge', 'technical');

insert into subjects (slug, name, subject_group, metadata)
values ('decision-making', 'Decision Making', 'reasoning', '{"exams":["nabard"]}'::jsonb);

select id, slug, name, subject_group, metadata from subjects
where slug in ('computer-knowledge','decision-making');
commit;

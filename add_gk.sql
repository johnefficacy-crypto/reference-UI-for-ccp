insert into subjects (slug, name, subject_group)
values ('general-knowledge', 'General Knowledge', 'general_studies')
on conflict (slug) do nothing;

insert into topics (subject_id, parent_topic_id, slug, name, level)
select s.id, null, v.slug, v.name, 'topic'
from subjects s, (values
  ('gk-history','History'),
  ('gk-geography','Geography'),
  ('gk-polity','Indian Polity and Constitution'),
  ('gk-economy','Indian Economy'),
  ('gk-general-science','General Science'),
  ('gk-miscellaneous','Miscellaneous')
) as v(slug, name)
where s.slug = 'general-knowledge'
on conflict (subject_id, parent_topic_id, slug) do nothing;

select s.slug, t.id, t.slug, t.name, t.level
from topics t join subjects s on s.id = t.subject_id
where s.slug = 'general-knowledge' order by t.name;
insert into topics (subject_id, parent_topic_id, slug, name, level)
select id, null, 'gk-international-relations', 'International Relations', 'topic'
from subjects where slug = 'general-knowledge';

update topics set parent_topic_id = (select id from topics where slug='gk-international-relations')
where slug in (
  'gk-international-organisations-and-groupings-ccdfea5b',
  'gk-international-agreements-and-declarations-19824d81',
  'gk-international-summits-and-forums-328d7104'
);

select p.name as section, count(*) from topics t join topics p on p.id=t.parent_topic_id
where t.subject_id=(select id from subjects where slug='general-knowledge') group by 1 order by 2 desc;
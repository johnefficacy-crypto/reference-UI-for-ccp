-- 272_pyq_projection_bytea_cast_fix.sql
-- PYQ -> mock_question_bank projection: stop routing the content hash through
-- the bytea escape parser.
--
-- BUG: the content hash in project_pyq_question_to_mock_bank has always been
-- computed as `sha256((<concatenated text>)::bytea)`. `text::bytea` in
-- PostgreSQL is not a byte reinterpretation — it is an I/O cast through
-- `byteain`, which parses the text as a bytea *input literal* in escape format.
-- There, a backslash introduces an escape: only `\\` (one literal backslash) and
-- `\ooo` (an octal byte) are legal. Any other backslash raises
--
--     ERROR: invalid input syntax for type bytea
--
-- and aborts the RPC, so sync dies on that row with earlier rows already
-- committed. Observed on RBI Grade B 2024 Phase I question
-- fb7fa071-14ec-4930-9d9d-a846f0c18985 (Q95), whose question_text contains
-- `attention.\ B` — a lone backslash followed by a space, which is not a legal
-- escape.
--
-- This is the same failure class migration 239 fixed for chr(0): a character the
-- cast cannot accept aborts the whole expression. 239 swapped the crashing
-- separator but left the cast, so every backslash in every projected field
-- (question_text, explanation_text, option_text, stimulus content_text, source
-- URLs) stayed a live landmine.
--
-- It was also silently wrong when it did not raise. `::bytea` collapses `\\` to
-- one byte and `\101` to `A`, while the Python mirror
-- compute_content_hash() hashes `raw.encode("utf-8")`
-- (app/backend/app/admin/pyq_mock_projection.py:363). Any backslash-bearing text
-- therefore hashed differently on the two sides, so preview reported
-- "would_update" forever and sync re-projected the row on every run.
--
-- FIX: `sha256(convert_to(<expr>, 'UTF8'))` — the same idiom already used in
-- 209_english_writing_practice_evaluator.sql:1691. convert_to performs an
-- encoding conversion with no escape parsing, so the bytes hashed are exactly
-- the UTF-8 bytes of the text, matching the Python mirror byte for byte.
--
-- HASH CONTINUITY: for text with no backslash, convert_to(t,'UTF8') and t::bytea
-- produce identical bytes, so already-projected rows keep their hash and stay
-- "unchanged". Only rows containing a backslash change hash — and those could
-- never have been projected at all, because the cast raised on them.
--
-- 270 is MERGED and IMMUTABLE. This is a forward migration that
-- `create or replace`s the projection RPC with 270's body verbatim; the only
-- change is the cast. Field set, field order and separators are untouched.
--
-- SLOT NOTE: 272 = MAX(filesystem) + 1 after 271_review_pyq_paper_question_count_gate.sql.
-- VERIFY against the target before rollout (SELECT MAX(version) FROM schema_migrations);
-- do not assume from the filename alone.

create or replace function public.project_pyq_question_to_mock_bank(
    p_pyq_question_id uuid,
    p_actor_id        uuid,
    p_audit_reason    text
) returns jsonb
language plpgsql
security definer
set search_path = public
as $fn$
declare
    v_q          record;
    v_primary_tag record;
    v_topic      record;

    -- Resolved projection targets for the single verified primary tag.
    -- v_topic_id is always the top-level topic; v_microtopic_id is the tagged
    -- microtopic, or NULL when the tag already points at a top-level topic.
    v_topic_id      uuid;
    v_microtopic_id uuid;

    v_content_hash        text;
    v_correct_count       integer;
    v_option_count        integer;
    v_verified_opt_count  integer;
    v_empty_opt_count     integer;
    v_primary_tag_count   integer;

    v_projection record;
    v_mock_q_id  uuid;
    v_is_new     boolean := false;
    v_outcome    text;

    v_correct_opt_id uuid;

    v_opt_row    record;
    v_new_opt_id uuid;
begin
    if p_audit_reason is null or length(trim(p_audit_reason)) < 8 then
        return jsonb_build_object(
            'outcome', 'error',
            'error',   'audit_reason_required',
            'detail',  'p_audit_reason must be at least 8 non-blank characters'
        );
    end if;

    select
        q.id,
        q.pyq_paper_id,
        q.question_number,
        q.question_text,
        q.question_type,
        q.correct_option_id     as pyq_correct_option_id,
        q.explanation_text,
        q.observed_difficulty,
        q.expected_solve_time_sec,
        q.language,
        q.section_id,
        q.reviewer_status       as q_reviewer_status,
        q.metadata              as q_metadata,
        p.exam_id,
        p.year                  as paper_year,
        p.trust_status          as paper_trust_status,
        p.source_url            as paper_source_url,
        p.source_type           as paper_source_type,
        p.source_document_id    as paper_source_document_id
    into v_q
    from public.pyq_questions q
    join public.pyq_papers    p on p.id = q.pyq_paper_id
    where q.id = p_pyq_question_id
    for update of q, p;

    if not found then
        return jsonb_build_object(
            'outcome', 'error',
            'error',   'question_not_found',
            'pyq_question_id', p_pyq_question_id
        );
    end if;

    if v_q.paper_trust_status != 'verified' then
        perform public.fn_block_projection_for_question(p_pyq_question_id, 'paper_not_verified');
        return jsonb_build_object(
            'outcome',            'blocked',
            'reason',             'paper_not_verified',
            'paper_trust_status', v_q.paper_trust_status,
            'pyq_question_id',    p_pyq_question_id
        );
    end if;

    -- 2a. Revalidate source_document_id when present (defence-in-depth, from 187).
    --     review_pyq_paper() validates under lock at verification time; this step
    --     catches documents that were archived after the paper was verified.
    if v_q.paper_source_document_id is not null then
        if not exists (
            select 1 from public.document_assets
            where id            = v_q.paper_source_document_id
              and scope         = 'admin_exam_intelligence'
              and document_kind = 'pyq_paper'
              and status        not in ('failed', 'archived')
              and coalesce(trim(storage_bucket), '') != ''
              and coalesce(trim(storage_path),  '') != ''
        ) then
            perform public.fn_block_projection_for_question(p_pyq_question_id, 'source_document_invalid');
            return jsonb_build_object(
                'outcome',            'blocked',
                'reason',             'source_document_invalid',
                'source_document_id', v_q.paper_source_document_id,
                'pyq_question_id',    p_pyq_question_id
            );
        end if;
    end if;

    if v_q.q_reviewer_status != 'verified' then
        perform public.fn_block_projection_for_question(p_pyq_question_id, 'question_not_verified');
        return jsonb_build_object(
            'outcome',         'blocked',
            'reason',          'question_not_verified',
            'reviewer_status', v_q.q_reviewer_status,
            'pyq_question_id', p_pyq_question_id
        );
    end if;

    if v_q.question_type != 'mcq' then
        perform public.fn_block_projection_for_question(p_pyq_question_id, 'not_mcq');
        return jsonb_build_object(
            'outcome',         'blocked',
            'reason',          'not_mcq',
            'question_type',   v_q.question_type,
            'pyq_question_id', p_pyq_question_id
        );
    end if;

    if coalesce(trim(v_q.question_text), '') = '' then
        perform public.fn_block_projection_for_question(p_pyq_question_id, 'empty_question_text');
        return jsonb_build_object(
            'outcome',         'blocked',
            'reason',          'empty_question_text',
            'pyq_question_id', p_pyq_question_id
        );
    end if;

    select
        count(*)                                                             as total,
        count(*) filter (where reviewer_status = 'verified')                as verified_count,
        count(*) filter (where reviewer_status = 'verified' and is_correct) as correct_verified_count,
        count(*) filter (
            where reviewer_status = 'verified'
              and coalesce(trim(option_text), '') = ''
        )                                                                    as empty_text_count
    into v_option_count, v_verified_opt_count, v_correct_count, v_empty_opt_count
    from public.pyq_options
    where question_id = p_pyq_question_id;

    if v_verified_opt_count < 2 then
        perform public.fn_block_projection_for_question(p_pyq_question_id, 'insufficient_verified_options');
        return jsonb_build_object(
            'outcome',               'blocked',
            'reason',                'insufficient_verified_options',
            'verified_option_count', v_verified_opt_count,
            'pyq_question_id',       p_pyq_question_id
        );
    end if;

    if v_empty_opt_count > 0 then
        perform public.fn_block_projection_for_question(p_pyq_question_id, 'empty_verified_option_text');
        return jsonb_build_object(
            'outcome',         'blocked',
            'reason',          'empty_verified_option_text',
            'empty_opt_count', v_empty_opt_count,
            'pyq_question_id', p_pyq_question_id
        );
    end if;

    if v_correct_count != 1 then
        perform public.fn_block_projection_for_question(p_pyq_question_id, 'not_exactly_one_verified_correct_option');
        return jsonb_build_object(
            'outcome',       'blocked',
            'reason',        'not_exactly_one_verified_correct_option',
            'correct_count', v_correct_count,
            'pyq_question_id', p_pyq_question_id
        );
    end if;

    if v_q.pyq_correct_option_id is not null then
        if not exists (
            select 1 from public.pyq_options
            where question_id     = p_pyq_question_id
              and reviewer_status = 'verified'
              and is_correct      = true
              and id              = v_q.pyq_correct_option_id
        ) then
            perform public.fn_block_projection_for_question(p_pyq_question_id, 'correct_option_id_mismatch');
            return jsonb_build_object(
                'outcome',           'blocked',
                'reason',            'correct_option_id_mismatch',
                'stated_correct_id', v_q.pyq_correct_option_id,
                'pyq_question_id',   p_pyq_question_id
            );
        end if;
    end if;

    select count(*)
    into v_primary_tag_count
    from public.pyq_question_topic_tags
    where question_id     = p_pyq_question_id
      and tag_role        = 'primary'
      and reviewer_status = 'verified';

    if v_primary_tag_count != 1 then
        perform public.fn_block_projection_for_question(p_pyq_question_id, 'primary_topic_tag_count_not_one');
        return jsonb_build_object(
            'outcome',           'blocked',
            'reason',            'primary_topic_tag_count_not_one',
            'primary_tag_count', v_primary_tag_count,
            'pyq_question_id',   p_pyq_question_id
        );
    end if;

    select t.*
    into v_primary_tag
    from public.pyq_question_topic_tags t
    where t.question_id     = p_pyq_question_id
      and t.tag_role        = 'primary'
      and t.reviewer_status = 'verified'
    limit 1;

    select s.*
    into v_topic
    from public.topics s
    where s.id = v_primary_tag.topic_id
      and s.is_active = true
    limit 1;

    if not found then
        perform public.fn_block_projection_for_question(p_pyq_question_id, 'primary_topic_invalid_or_inactive');
        return jsonb_build_object(
            'outcome',         'blocked',
            'reason',          'primary_topic_invalid_or_inactive',
            'topic_id',        v_primary_tag.topic_id,
            'pyq_question_id', p_pyq_question_id
        );
    end if;

    if v_topic.subject_id is null then
        perform public.fn_block_projection_for_question(p_pyq_question_id, 'primary_topic_has_no_subject');
        return jsonb_build_object(
            'outcome',         'blocked',
            'reason',          'primary_topic_has_no_subject',
            'topic_id',        v_primary_tag.topic_id,
            'pyq_question_id', p_pyq_question_id
        );
    end if;

    -- Resolve the primary tag's LEVEL from topics.parent_topic_id and split it
    -- across the two bank columns. Before this migration the projection wrote
    -- only topic_id = v_primary_tag.topic_id, so a tag pointing at a microtopic
    -- was flattened into topic_id and microtopic_id was left NULL — the
    -- microtopic was lost for every such question.
    --
    --   parent_topic_id IS NULL  -> top-level topic:
    --       topic_id = tag topic_id, microtopic_id = NULL
    --   parent_topic_id NOT NULL -> microtopic:
    --       microtopic_id = tag topic_id, topic_id = that row's parent_topic_id
    --
    -- Only ONE level of promotion is performed, matching the taxonomy's two
    -- practical levels (topics.level in 'topic'/'microtopic', migration 029:35).
    -- A deeper 'concept' row would land its immediate parent in topic_id; that
    -- is the existing taxonomy contract and is not widened here.
    if v_topic.parent_topic_id is null then
        v_topic_id      := v_primary_tag.topic_id;
        v_microtopic_id := null;
    else
        v_microtopic_id := v_primary_tag.topic_id;
        v_topic_id      := v_topic.parent_topic_id;
    end if;

    -- Stimulus verification gate (PR-4, conjunctive trust): if the question has
    -- any pyq_question_stimuli link, EVERY such link AND its referenced
    -- pyq_stimuli must be 'verified'. A question with no links is unaffected.
    -- LEFT JOIN so a dangling link (missing stimulus) also counts as unverified.
    if exists (
        select 1
        from public.pyq_question_stimuli qs
        left join public.pyq_stimuli s on s.id = qs.stimulus_id
        where qs.question_id = p_pyq_question_id
          and (
               qs.reviewer_status is distinct from 'verified'
            or s.id is null
            or s.reviewer_status is distinct from 'verified'
          )
    ) then
        perform public.fn_block_projection_for_question(p_pyq_question_id, 'stimulus_not_verified');
        return jsonb_build_object(
            'outcome',         'blocked',
            'reason',          'stimulus_not_verified',
            'pyq_question_id', p_pyq_question_id
        );
    end if;

    -- Hash = SHA256 over all projected fields.
    -- Matches compute_content_hash() in pyq_mock_projection.py.
    -- Top-level fields are joined with chr(29) (GS); list items with chr(31) (US)
    -- and within-item parts with chr(30) (RS). chr(0)/NUL is NOT used — a null
    -- byte is illegal in PostgreSQL text and would abort this expression (and the
    -- whole projection/sync). Field set + order preserved verbatim from migration
    -- 229; only the crashing top-level separator was swapped (chr(0) → chr(29)).
    -- PR-4 appended (after the existing fields, order preserved): section_id,
    -- per-verified-option source_label+display_order (same verified-option
    -- ordering), per-verified-stimulus type+content+language+link display_order.
    select encode(
        sha256(convert_to((
            coalesce(lower(trim(v_q.question_text)), '') || chr(29) ||
            coalesce(lower(trim(v_q.explanation_text)), '') || chr(29) ||
            (case when lower(trim(coalesce(v_q.observed_difficulty, ''))) in ('easy','medium','hard')
                  then lower(trim(v_q.observed_difficulty)) else 'medium' end) || chr(29) ||
            coalesce(nullif(lower(trim(coalesce(v_q.language, ''))), ''), 'en') || chr(29) ||
            coalesce(v_q.expected_solve_time_sec::text, '') || chr(29) ||
            coalesce(v_q.pyq_paper_id::text, '') || chr(29) ||
            coalesce(v_q.paper_year::text, '') || chr(29) ||
            coalesce(v_q.exam_id::text, '') || chr(29) ||
            coalesce(v_q.paper_source_url, '') || chr(29) ||
            coalesce(v_q.paper_source_type, '') || chr(29) ||
            coalesce(v_q.paper_source_document_id::text, '') || chr(29) ||
            coalesce((
                select string_agg(
                    coalesce(lower(o.option_label), '') || chr(30) ||
                    coalesce(lower(trim(o.option_text)), ''),
                    chr(31) order by coalesce(lower(o.option_label), ''), o.id
                )
                from public.pyq_options o
                where o.question_id   = p_pyq_question_id
                  and o.reviewer_status = 'verified'
            ), '') || chr(29) ||
            coalesce((
                select lower(trim(c.option_text))
                from public.pyq_options c
                where c.question_id   = p_pyq_question_id
                  and c.reviewer_status = 'verified'
                  and c.is_correct    = true
                limit 1
            ), '') || chr(29) ||
            coalesce((
                select string_agg(
                    coalesce(t.topic_id::text, '') || chr(30) ||
                    coalesce(t.tag_role, ''),
                    chr(31) order by t.topic_id, t.tag_role
                )
                from public.pyq_question_topic_tags t
                where t.question_id   = p_pyq_question_id
                  and t.reviewer_status = 'verified'
            ), '') || chr(29) ||
            coalesce(v_q.section_id::text, '') || chr(29) ||
            coalesce((
                select string_agg(
                    coalesce(o.source_label, '') || chr(30) ||
                    coalesce(o.display_order::text, ''),
                    chr(31) order by coalesce(lower(o.option_label), ''), o.id
                )
                from public.pyq_options o
                where o.question_id   = p_pyq_question_id
                  and o.reviewer_status = 'verified'
            ), '') || chr(29) ||
            coalesce((
                select string_agg(
                    coalesce(s.stimulus_type, '') || chr(30) ||
                    coalesce(s.content_text, '') || chr(30) ||
                    coalesce(s.language, '') || chr(30) ||
                    coalesce(qs.display_order::text, ''),
                    chr(31) order by qs.display_order nulls last, s.display_order nulls last, s.id
                )
                from public.pyq_question_stimuli qs
                join public.pyq_stimuli s on s.id = qs.stimulus_id
                where qs.question_id     = p_pyq_question_id
                  and qs.reviewer_status = 'verified'
                  and s.reviewer_status  = 'verified'
            ), '') || chr(29) ||
            coalesce(v_microtopic_id::text, '')
        ), 'UTF8')),
        'hex'
    ) into v_content_hash;

    select * into v_projection
    from public.pyq_mock_question_projections
    where pyq_question_id = p_pyq_question_id
    for update;

    if found then
        v_mock_q_id := v_projection.mock_question_id;

        declare
            v_existing_pyq_q_id uuid;
        begin
            select pyq_question_id
            into v_existing_pyq_q_id
            from public.mock_question_bank
            where id = v_mock_q_id;

            if not found then
                update public.pyq_mock_question_projections
                set sync_status = 'archived', updated_at = now()
                where pyq_question_id = p_pyq_question_id;
                v_mock_q_id  := null;
                v_projection := null;
                v_is_new     := true;
            elsif v_existing_pyq_q_id is not null
                  and v_existing_pyq_q_id != p_pyq_question_id then
                return jsonb_build_object(
                    'outcome',            'conflict',
                    'error',              'mock_question_linked_to_different_pyq',
                    'mock_question_id',   v_mock_q_id,
                    'conflicting_pyq_id', v_existing_pyq_q_id,
                    'pyq_question_id',    p_pyq_question_id
                );
            end if;
        end;

        if v_projection is not null
           and v_projection.sync_status = 'active'
           and v_projection.source_content_hash = v_content_hash then
            v_outcome := 'unchanged';
        else
            v_outcome := 'updated';
        end if;
    else
        v_is_new    := true;
        v_mock_q_id := gen_random_uuid();
        v_outcome   := 'created';
    end if;

    if v_outcome = 'unchanged' then
        update public.pyq_mock_question_projections
        set updated_at       = now(),
            last_sync_result = jsonb_build_object(
                'outcome',      'unchanged',
                'checked_at',   now()::text,
                'content_hash', v_content_hash
            )
        where pyq_question_id = p_pyq_question_id;

        return jsonb_build_object(
            'outcome',          'unchanged',
            'mock_question_id', v_mock_q_id,
            'pyq_question_id',  p_pyq_question_id,
            'content_hash',     v_content_hash
        );
    end if;

    if v_is_new then
        insert into public.mock_question_bank (
            id, exam_id, subject_id, topic_id, microtopic_id, section_id, question_text, question_type,
            difficulty, explanation, language, reviewer_status, published_at,
            source_type, source_kind, question_fingerprint, pyq_question_id,
            pyq_paper_id, pyq_year, expected_time_sec, created_by, created_at, updated_at
        ) values (
            v_mock_q_id, v_q.exam_id, v_topic.subject_id, v_topic_id, v_microtopic_id, v_q.section_id,
            v_q.question_text, 'mcq',
            case when lower(v_q.observed_difficulty) in ('easy','medium','hard')
                 then lower(v_q.observed_difficulty) else 'medium' end,
            v_q.explanation_text, coalesce(v_q.language, 'en'), 'published', now(),
            'pyq', 'pyq', v_content_hash, p_pyq_question_id,
            v_q.pyq_paper_id, v_q.paper_year, v_q.expected_solve_time_sec,
            p_actor_id, now(), now()
        );
    else
        update public.mock_question_bank set
            exam_id              = v_q.exam_id,
            subject_id           = v_topic.subject_id,
            topic_id             = v_topic_id,
            microtopic_id        = v_microtopic_id,
            section_id           = v_q.section_id,
            question_text        = v_q.question_text,
            question_type        = 'mcq',
            difficulty           = case when lower(v_q.observed_difficulty) in ('easy','medium','hard')
                                       then lower(v_q.observed_difficulty) else 'medium' end,
            explanation          = v_q.explanation_text,
            language             = coalesce(v_q.language, 'en'),
            reviewer_status      = 'published',
            published_at         = coalesce(
                                       (select published_at from public.mock_question_bank where id = v_mock_q_id),
                                       now()
                                   ),
            source_type          = 'pyq',
            source_kind          = 'pyq',
            question_fingerprint = v_content_hash,
            pyq_question_id      = p_pyq_question_id,
            pyq_paper_id         = v_q.pyq_paper_id,
            pyq_year             = v_q.paper_year,
            expected_time_sec    = v_q.expected_solve_time_sec,
            updated_at           = now()
        where id = v_mock_q_id;
    end if;

    delete from public.mock_question_options where question_id = v_mock_q_id;

    v_correct_opt_id := null;

    for v_opt_row in
        select id, option_text, option_label, is_correct, source_label, display_order,
               row_number() over (order by option_label, id) - 1 as opt_idx
        from public.pyq_options
        where question_id     = p_pyq_question_id
          and reviewer_status = 'verified'
        order by option_label, id
    loop
        insert into public.mock_question_options (
            question_id, option_text, option_index, is_correct, source_label, display_order
        ) values (
            v_mock_q_id, v_opt_row.option_text, v_opt_row.opt_idx, v_opt_row.is_correct,
            v_opt_row.source_label, v_opt_row.display_order
        )
        returning id into v_new_opt_id;

        if v_opt_row.is_correct then
            v_correct_opt_id := v_new_opt_id;
        end if;
    end loop;

    update public.mock_question_bank
    set correct_option_id = v_correct_opt_id, updated_at = now()
    where id = v_mock_q_id;

    -- Snapshot the question's VERIFIED shared stimuli (PR-4). Replace-all so a
    -- removed/unverified stimulus does not linger in the projected snapshot.
    delete from public.mock_question_stimuli where mock_question_id = v_mock_q_id;

    insert into public.mock_question_stimuli (
        mock_question_id, pyq_stimulus_id, stimulus_type, content_text, language, display_order
    )
    select
        v_mock_q_id, s.id, s.stimulus_type, s.content_text, s.language, qs.display_order
    from public.pyq_question_stimuli qs
    join public.pyq_stimuli s on s.id = qs.stimulus_id
    where qs.question_id     = p_pyq_question_id
      and qs.reviewer_status = 'verified'
      and s.reviewer_status  = 'verified'
    order by qs.display_order nulls last, s.display_order nulls last, s.id;

    delete from public.mock_question_topic_tags where question_id = v_mock_q_id;

    insert into public.mock_question_topic_tags (question_id, topic_id, role)
    select v_mock_q_id, t.topic_id, t.tag_role
    from public.pyq_question_topic_tags t
    where t.question_id   = p_pyq_question_id
      and t.reviewer_status = 'verified';

    delete from public.mock_question_sources where question_id = v_mock_q_id;

    insert into public.mock_question_sources (
        question_id, source_kind, source_trust, source_url,
        pyq_paper_id, pyq_year, evidence_text, source_document_id
    ) values (
        v_mock_q_id, 'pyq', 'verified', v_q.paper_source_url,
        v_q.pyq_paper_id, v_q.paper_year,
        'projected_from_pyq_question_id:' || p_pyq_question_id::text,
        v_q.paper_source_document_id
    );

    insert into public.pyq_mock_question_projections (
        pyq_question_id, mock_question_id, source_content_hash, sync_status,
        last_sync_result, projected_by, projected_at, updated_at
    ) values (
        p_pyq_question_id, v_mock_q_id, v_content_hash, 'active',
        jsonb_build_object('outcome', v_outcome, 'projected_at', now()::text),
        p_actor_id, now(), now()
    )
    on conflict (pyq_question_id) do update
      set mock_question_id    = excluded.mock_question_id,
          source_content_hash = excluded.source_content_hash,
          sync_status         = 'active',
          last_sync_result    = excluded.last_sync_result,
          projected_by        = excluded.projected_by,
          projected_at        = case
              when pyq_mock_question_projections.projected_at is null
              then excluded.projected_at
              else pyq_mock_question_projections.projected_at
          end,
          updated_at          = now();

    insert into public.admin_audit_logs (
        actor_id, action, entity_type, entity_id, new_value, notes
    ) values (
        p_actor_id,
        'pyq_mock_projection_sync',
        'mock_question_bank',
        v_mock_q_id::text,
        jsonb_build_object(
            'outcome',         v_outcome,
            'pyq_question_id', p_pyq_question_id,
            'pyq_paper_id',    v_q.pyq_paper_id,
            'exam_id',         v_q.exam_id,
            'pyq_year',        v_q.paper_year,
            'content_hash',    v_content_hash,
            'topic_id',        v_topic_id,
            'microtopic_id',   v_microtopic_id,
            'subject_id',      v_topic.subject_id
        ),
        p_audit_reason
    );

    return jsonb_build_object(
        'outcome',          v_outcome,
        'mock_question_id', v_mock_q_id,
        'pyq_question_id',  p_pyq_question_id,
        'content_hash',     v_content_hash,
        'is_new',           v_is_new
    );

exception
    when others then raise;
end;
$fn$;

-- ── Security: preserve the RPC's service-role-only posture (matches 184/229) ──

revoke all on function public.project_pyq_question_to_mock_bank(uuid, uuid, text) from public;
revoke execute on function public.project_pyq_question_to_mock_bank(uuid, uuid, text) from anon;
revoke execute on function public.project_pyq_question_to_mock_bank(uuid, uuid, text) from authenticated;
grant execute on function public.project_pyq_question_to_mock_bank(uuid, uuid, text) to service_role;

notify pgrst, 'reload schema';

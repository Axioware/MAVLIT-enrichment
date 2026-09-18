DO $$
BEGIN
    IF to_regclass('public.creator_profiles_test') IS NOT NULL
       AND to_regclass('public.creator_niches') IS NULL THEN
        ALTER TABLE creator_profiles_test RENAME TO creator_niches;
    END IF;
END
$$;

    description TEXT,
        DELETE FROM prompts
        WHERE name IN ('creator_profile', 'instagram_creator_profile')
          AND name <> 'creator_niche_description';
CREATE INDEX IF NOT EXISTS ix_creator_profiles_test_username
    ON creator_profiles_test (username);
CREATE INDEX IF NOT EXISTS ix_creator_profiles_test_niche
    ON creator_profiles_test (niche);
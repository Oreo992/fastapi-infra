def test_task4_cleanup_keeps_internal_modules_importable():
    import infra.database.repository  # noqa: F401
    import infra.middleware.error_handler  # noqa: F401
    import infra.middleware.middleware  # noqa: F401
    import infra.streaming.streams_manager  # noqa: F401

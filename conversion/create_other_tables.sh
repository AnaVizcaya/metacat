#!/bin/sh

source ./config.sh

$OUT_DB_PSQL << _EOF_

drop table if exists 
    queries
    ,files_datasets
    ,datasets
    ,authenticators
    ,parameter_definitions
    ,namespaces
;

create table authenticators
(
    username    text    references users(username) on delete cascade,
    type        text
        constraint authenticator_types check ( 
            type in ('x509','password','ssh')
            ),
    secrets      text[],
    primary key(username, type)
);

create table namespaces
(
    name                text        primary key,
    owner               text        references  roles(name),
    creator        text references users(username),
    created_timestamp   timestamp with time zone        default now()
);

insert into namespaces(name, owner, creator)
(
    select distinct namespace, 'admin_role', 'admin' from files
);

insert into namespaces(name, owner, creator) values('dune', 'admin_role', 'admin');

create table datasets
(
    namespace           text references namespaces(name),
    name                text,

    primary key (namespace, name),

    parent_namespace    text,
    parent_name         text,

    foreign key (parent_namespace, parent_name) references datasets(namespace, name),

    frozen		boolean default 'false',
    monotonic		boolean default 'false',
    metadata    jsonb   default '{}',
    required_metadata   text[],
    creator        text references users(username),
    created_timestamp   timestamp with time zone     default now(),
    expiration          timestamp with time zone,
    description         text
);

insert into datasets(namespace, name, creator, description)
	values('dune','all','admin','All files imported during conversion from SAM');


create table files_datasets
(
    file_id                 text,
    dataset_namespace       text,
    dataset_name            text
);       

\echo Populating dataset "all" ...

insert into files_datasets(file_id, dataset_namespace, dataset_name)
(
	select f.id, 'dune','all'
		from files f
);

create table queries
(
    namespace       text references namespaces(name),
    name            text,
    parameters      text[],
    source      text,
    primary key(namespace, name),
    creator             text references users(username),
    created_timestamp   timestamp with time zone     default now()
);

create table parameter_definitions
(
    category    text    references parameter_categories(path),
    name        text,
    type        text
        constraint attribute_types check ( 
            type in ('int','double','text','boolean',
                    'int array','double array','text array','boolean array')
            ),
    int_values      bigint[],
    int_min         bigint,
    int_max         bigint,
    double_values   double precision[],
    double_min      double precision,
    double_max      double precision,
    text_values     text[],
    text_pattern    text,
    bollean_value   boolean,
    required        boolean,
    creator             text references users(username),
    created_timestamp   timestamp with time zone        default now(),
    primary key(category, name)
);

    


_EOF_




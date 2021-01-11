#!/bin/sh


source ./config.sh

$IN_DB_PSQL -q > ./data/lineages.csv << _EOF_

copy (	select distinct l.file_id_source, l.file_id_dest
		from file_lineages l, data_files f1, data_files f2
		where f1.file_id = l.file_id_source and f1.retired_date is null 
			and f2.file_id = l.file_id_dest and f2.retired_date is null
) to stdout



_EOF_


$OUT_DB_PSQL << _EOF_

drop table if exists parent_child;

create table parent_child
(
	parent_id text,
	child_id text
);

create temp table parent_child_temp
(
    like parent_child
);

\echo ... loading ...

\copy parent_child_temp(parent_id, child_id) from 'data/lineages.csv';

insert into parent_child(parent_id, child_id)
(
    select t.parent_id, t.child_id
    from parent_child_temp t
    inner join raw_files f1 on f1.file_id = t.parent_id
    inner join raw_files f2 on f2.file_id = t.child_id
);

\echo ... creating primary key ...



alter table parent_child add primary key(parent_id, child_id);




_EOF_

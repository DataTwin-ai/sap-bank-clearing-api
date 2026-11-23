FUNCTION z_invoice_post
  IMPORTING
    VALUE(iv_bukrs) TYPE bukrs
    VALUE(iv_kunnr) TYPE kunnr
    VALUE(iv_bldat) TYPE bldat
    VALUE(iv_budat) TYPE budat
    VALUE(iv_blart) TYPE blart DEFAULT 'DR'
    VALUE(iv_monat) TYPE monat OPTIONAL
    VALUE(iv_waers) TYPE waers
    VALUE(iv_wrbtr) TYPE wrbtr
    VALUE(iv_hkont) TYPE hkont
    VALUE(iv_mwskz) TYPE mwskz OPTIONAL
    VALUE(iv_xblnr) TYPE xblnr1 OPTIONAL
    VALUE(iv_zuonr) TYPE dzuonr OPTIONAL
    VALUE(iv_bktxt) TYPE bktxt OPTIONAL
    VALUE(iv_sgtxt) TYPE sgtxt OPTIONAL
    VALUE(iv_zfbdt) TYPE dzfbdt OPTIONAL
    VALUE(iv_simulate) TYPE char1 OPTIONAL
  EXPORTING
    VALUE(ev_status) TYPE char1
    VALUE(ev_belnr) TYPE belnr_d
    VALUE(ev_gjahr) TYPE gjahr
    VALUE(ev_message) TYPE bapi_msg.
*----------------------------------------------------------------------
* Customer invoice posting (FB70 / FB01 logic) for DataTwin AR Inbox.
*   line 1: posting key 01, customer IV_KUNNR, gross amount (debit)
*   line 2: posting key 50, revenue G/L IV_HKONT, gross amount (credit)
*   IV_MWSKZ given: tax calculated by SAP (BKPF-XMWST = X)
*   IV_SIMULATE = X: FB01 runs up to the simulation screen, nothing is saved
*     (POSTING_INTERFACE_DOCUMENT I_XSIMU replaces the save code /11 by BS)
* RFC-enabled; called by DataTwin via /sap/bc/soap/rfc.
* EV_STATUS: S = posted
*            V = simulation passed, nothing posted
*            D = reference IV_XBLNR already posted (EV_BELNR = that document)
*            E = error, nothing posted
*----------------------------------------------------------------------

  DATA: lt_ftpost TYPE STANDARD TABLE OF ftpost,
        lt_fttax  TYPE STANDARD TABLE OF fttax,
        lt_blntab TYPE STANDARD TABLE OF blntab,
        ls_blntab TYPE blntab,
        lv_kunnr  TYPE kunnr,
        lv_hkont  TYPE hkont,
        lv_date   TYPE char10,
        lv_amount TYPE char20,
        lv_msgid  TYPE sy-msgid,
        lv_msgno  TYPE sy-msgno,
        lv_msgty  TYPE sy-msgty,
        lv_msgv1  TYPE sy-msgv1,
        lv_msgv2  TYPE sy-msgv2,
        lv_msgv3  TYPE sy-msgv3,
        lv_msgv4  TYPE sy-msgv4,
        lv_subrc  TYPE sy-subrc.

  CLEAR: ev_status, ev_belnr, ev_gjahr, ev_message.

  CALL FUNCTION 'CONVERSION_EXIT_ALPHA_INPUT'
    EXPORTING input  = iv_kunnr
    IMPORTING output = lv_kunnr.
  CALL FUNCTION 'CONVERSION_EXIT_ALPHA_INPUT'
    EXPORTING input  = iv_hkont
    IMPORTING output = lv_hkont.

*--------------------------------------------------------------------*
* 1. Duplicate check: same reference already posted (not reversed)
*--------------------------------------------------------------------*
  IF iv_xblnr IS NOT INITIAL.
    SELECT SINGLE belnr, gjahr FROM bkpf
      INTO (@ev_belnr, @ev_gjahr)
      WHERE bukrs = @iv_bukrs
        AND blart = @iv_blart
        AND xblnr = @iv_xblnr
        AND stblg = @space.
    IF sy-subrc = 0.
      ev_status  = 'D'.
      ev_message = |Reference { iv_xblnr } already posted in document { ev_belnr }/{ ev_gjahr }|.
      RETURN.
    ENDIF.
  ENDIF.

*--------------------------------------------------------------------*
* 2. Customer must exist in the company code
*--------------------------------------------------------------------*
  SELECT SINGLE @abap_true FROM knb1 INTO @DATA(lv_found)
    WHERE kunnr = @lv_kunnr AND bukrs = @iv_bukrs.
  IF sy-subrc <> 0.
    ev_status  = 'E'.
    ev_message = |Customer { lv_kunnr } is not created for company code { iv_bukrs } (KNB1)|.
    RETURN.
  ENDIF.

*--------------------------------------------------------------------*
* 3. Build posting data (values in user format, like batch input)
*--------------------------------------------------------------------*
  " Header (K)
  WRITE iv_bldat TO lv_date.
  APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-BLDAT' fval = lv_date ) TO lt_ftpost.
  WRITE iv_budat TO lv_date.
  APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-BUDAT' fval = lv_date ) TO lt_ftpost.
  APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-BLART' fval = iv_blart ) TO lt_ftpost.
  APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-BUKRS' fval = iv_bukrs ) TO lt_ftpost.
  APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-WAERS' fval = iv_waers ) TO lt_ftpost.
  IF iv_xblnr IS NOT INITIAL.
    APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-XBLNR' fval = iv_xblnr ) TO lt_ftpost.
  ENDIF.
  IF iv_bktxt IS NOT INITIAL.
    APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-BKTXT' fval = iv_bktxt ) TO lt_ftpost.
  ENDIF.
  IF iv_monat IS NOT INITIAL.
    APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-MONAT' fval = iv_monat ) TO lt_ftpost.
  ENDIF.
  IF iv_mwskz IS NOT INITIAL.
    APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-XMWST' fval = 'X' ) TO lt_ftpost.
  ENDIF.

  WRITE iv_wrbtr TO lv_amount CURRENCY iv_waers LEFT-JUSTIFIED.

  " Line 1: customer, posting key 01 (invoice, debit)
  APPEND VALUE #( stype = 'P' count = 1 fnam = 'RF05A-NEWBS' fval = '01' ) TO lt_ftpost.
  APPEND VALUE #( stype = 'P' count = 1 fnam = 'BSEG-KUNNR' fval = lv_kunnr ) TO lt_ftpost.
  APPEND VALUE #( stype = 'P' count = 1 fnam = 'BSEG-WRBTR' fval = lv_amount ) TO lt_ftpost.
  IF iv_zuonr IS NOT INITIAL.
    APPEND VALUE #( stype = 'P' count = 1 fnam = 'BSEG-ZUONR' fval = iv_zuonr ) TO lt_ftpost.
  ENDIF.
  IF iv_sgtxt IS NOT INITIAL.
    APPEND VALUE #( stype = 'P' count = 1 fnam = 'BSEG-SGTXT' fval = iv_sgtxt ) TO lt_ftpost.
  ENDIF.
  IF iv_zfbdt IS NOT INITIAL.
    WRITE iv_zfbdt TO lv_date.
    APPEND VALUE #( stype = 'P' count = 1 fnam = 'BSEG-ZFBDT' fval = lv_date ) TO lt_ftpost.
  ENDIF.

  " Line 2: revenue G/L, posting key 50 (credit)
  APPEND VALUE #( stype = 'P' count = 2 fnam = 'RF05A-NEWBS' fval = '50' ) TO lt_ftpost.
  APPEND VALUE #( stype = 'P' count = 2 fnam = 'BSEG-HKONT' fval = lv_hkont ) TO lt_ftpost.
  APPEND VALUE #( stype = 'P' count = 2 fnam = 'BSEG-WRBTR' fval = lv_amount ) TO lt_ftpost.
  IF iv_mwskz IS NOT INITIAL.
    APPEND VALUE #( stype = 'P' count = 2 fnam = 'BSEG-MWSKZ' fval = iv_mwskz ) TO lt_ftpost.
  ENDIF.
  IF iv_sgtxt IS NOT INITIAL.
    APPEND VALUE #( stype = 'P' count = 2 fnam = 'BSEG-SGTXT' fval = iv_sgtxt ) TO lt_ftpost.
  ENDIF.

*--------------------------------------------------------------------*
* 4. Post (FB01 logic, call transaction, no screens, synchronous)
*--------------------------------------------------------------------*
  CALL FUNCTION 'POSTING_INTERFACE_START'
    EXPORTING
      i_function         = 'C'
      i_mode             = 'N'
      i_update           = 'S'
      i_user             = sy-uname
    EXCEPTIONS
      client_incorrect   = 1
      function_invalid   = 2
      group_name_missing = 3
      mode_invalid       = 4
      update_invalid     = 5
      OTHERS             = 6.
  IF sy-subrc <> 0.
    ev_status  = 'E'.
    ev_message = |POSTING_INTERFACE_START failed (rc { sy-subrc })|.
    RETURN.
  ENDIF.

  CALL FUNCTION 'POSTING_INTERFACE_DOCUMENT'
    EXPORTING
      i_tcode                  = 'FB01'
      i_sgfunct                = 'C'
      i_xsimu                  = iv_simulate
    IMPORTING
      e_msgid                  = lv_msgid
      e_msgno                  = lv_msgno
      e_msgty                  = lv_msgty
      e_msgv1                  = lv_msgv1
      e_msgv2                  = lv_msgv2
      e_msgv3                  = lv_msgv3
      e_msgv4                  = lv_msgv4
      e_subrc                  = lv_subrc
    TABLES
      t_blntab                 = lt_blntab
      t_ftpost                 = lt_ftpost
      t_fttax                  = lt_fttax
    EXCEPTIONS
      account_missing          = 1
      company_code_missing     = 2
      posting_key_invalid      = 3
      posting_key_missing      = 4
      record_type_invalid      = 5
      transaction_code_invalid = 6
      amount_format_error      = 7
      too_many_line_items      = 8
      company_code_invalid     = 9
      screen_not_found         = 10
      no_authorization         = 11
      OTHERS                   = 12.
  DATA(lv_exc) = sy-subrc.

  CALL FUNCTION 'POSTING_INTERFACE_END'
    EXCEPTIONS
      session_not_processable = 1
      OTHERS                  = 2.

  IF lv_msgid IS NOT INITIAL.
    MESSAGE ID lv_msgid TYPE 'S' NUMBER lv_msgno
      WITH lv_msgv1 lv_msgv2 lv_msgv3 lv_msgv4 INTO ev_message.
  ENDIF.

  READ TABLE lt_blntab INTO ls_blntab INDEX 1.

  IF iv_simulate = 'X' AND ls_blntab-belnr IS INITIAL.
    " A clean simulation stops on the FB01 overview screen because no batch-input
    " data is left (00 344 for SAPMF05A 0700). Any other stop is an error.
    IF lv_exc = 0 AND lv_msgid = '00' AND lv_msgno = '344'
       AND lv_msgv1 = 'SAPMF05A' AND lv_msgv2 = '0700'.
      ev_status  = 'V'.
      ev_message = 'Simulation passed: SAP accepted all entries, nothing posted'.
    ELSE.
      ev_status = 'E'.
      IF ev_message IS INITIAL.
        ev_message = |Simulation failed (exception { lv_exc }, rc { lv_subrc })|.
      ELSE.
        ev_message = |Simulation stopped: { ev_message }|.
      ENDIF.
    ENDIF.
    RETURN.
  ENDIF.

  IF lv_exc <> 0 OR lv_subrc <> 0 OR ls_blntab-belnr IS INITIAL.
    ev_status = 'E'.
    IF ev_message IS INITIAL.
      ev_message = |Posting failed (exception { lv_exc }, rc { lv_subrc })|.
    ENDIF.
    RETURN.
  ENDIF.

  ev_status  = 'S'.
  ev_belnr   = ls_blntab-belnr.
  ev_gjahr   = ls_blntab-gjahr.
  ev_message = |Invoice document { ev_belnr }/{ ev_gjahr } posted|.

ENDFUNCTION.
